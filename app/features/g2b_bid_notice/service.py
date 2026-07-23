"""Direct G2B bid-notice preview service for the isolated feature."""

import hashlib
import os
import re
from datetime import datetime, time, timedelta
from typing import Any
from urllib.parse import unquote
from zoneinfo import ZoneInfo

import requests

from app.features.g2b_bid_notice.classifier import (
    apply_industry_restriction_exclusion,
    classify_bid_notice,
)
from app.features.g2b_bid_notice.contracts import (
    _parse_amount,
    _parse_kst_datetime,
    bid_notice_dedup_key,
    map_bid_notice_api_item,
)
from app.features.g2b_bid_notice.enrichment import (
    attachment_sources_from_notice_item,
    enrich_bid_notice_items,
    preview_enrichment_fields,
)
from app.features.g2b_bid_notice.query_history import append_query_history
from app.features.g2b_bid_notice.schemas import (
    BidNoticeCandidate,
    BidNoticePreviewItem,
    BidNoticePreviewRequest,
    BidNoticePreviewResponse,
    PersonalCollectionSettings,
)


G2B_API_BASE_URL = "https://apis.data.go.kr/1230000/ad/BidPublicInfoService"
PRIVATE_G2B_API_BASE_URL = "https://apis.data.go.kr/1230000/ao/PrvtBidNtceService"
KST = ZoneInfo("Asia/Seoul")

# General and technical services use the service listing endpoint for first-pass
# collection. Their exact G2B work-type metadata is still verified per notice;
# a missing raw value is never inferred from this endpoint choice.
WORK_TYPE_OPERATIONS = {
    "물품": "getBidPblancListInfoThngPPSSrch",
    "일반용역": "getBidPblancListInfoServcPPSSrch",
    "기술용역": "getBidPblancListInfoServcPPSSrch",
    "공사": "getBidPblancListInfoCnstwkPPSSrch",
}
PRIVATE_GOODS_WORK_TYPE = "민간물품"
PRIVATE_GOODS_OPERATION = "getPrvtBidPblancListInfoThng"
FOREIGN_OPERATION = "getBidPblancListInfoFrgcptPPSSrch"
OPERATION_WORK_TYPE_LABELS = {
    "getBidPblancListInfoThngPPSSrch": "물품",
    # The list endpoint combines normal and technical service notices, so it
    # must be shown as the neutral source-confirmed category "용역".
    "getBidPblancListInfoServcPPSSrch": "용역",
    "getBidPblancListInfoCnstwkPPSSrch": "공사",
    PRIVATE_GOODS_OPERATION: PRIVATE_GOODS_WORK_TYPE,
    FOREIGN_OPERATION: "외자",
}


def _clean_text(value: object) -> str | None:
    text = str("" if value is None else value).strip()
    return text or None


def _normalize_service_key(raw_key: str) -> str:
    """Accept either data.go.kr's Encoding or Decoding key without double encoding it."""

    key = raw_key.strip()
    for _ in range(2):
        decoded = unquote(key)
        if decoded == key:
            break
        key = decoded
    return key


def _extract_items(payload: object) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    response = payload.get("response")
    if not isinstance(response, dict):
        return []
    header = response.get("header") or {}
    result_code = str(header.get("resultCode") or "") if isinstance(header, dict) else ""
    if result_code and result_code != "00":
        message = str(header.get("resultMsg") or "알 수 없는 오류")
        raise ValueError(f"나라장터 API 오류({result_code}): {message}")
    body = response.get("body") or {}
    items = body.get("items") if isinstance(body, dict) else None
    if isinstance(items, dict):
        item = items.get("item")
        if isinstance(item, list):
            return [row for row in item if isinstance(row, dict)]
        if isinstance(item, dict):
            return [item]
    if isinstance(items, list):
        return [row for row in items if isinstance(row, dict)]
    return []


def _extract_total_count(payload: object) -> int | None:
    """Read the source-reported total without treating a missing value as zero."""

    if not isinstance(payload, dict):
        return None
    response = payload.get("response")
    if not isinstance(response, dict):
        return None
    body = response.get("body")
    if not isinstance(body, dict):
        return None
    return _parse_amount(body.get("totalCount"))


def _fetch_remaining_pages(
    *,
    operation_base_url: str,
    operation: str,
    first_page_params: dict[str, str | int],
    first_page_items: list[dict[str, Any]],
    reported_total_count: int | None,
    max_items: int | None = None,
) -> tuple[list[dict[str, Any]], dict[str, int | None]]:
    """Continue from page two until the public API's full result set is read."""

    api_page_size = int(first_page_params["numOfRows"])
    items = list(first_page_items[:max_items]) if max_items is not None else list(first_page_items)
    page_count = 1
    page_no = 1
    current_page_items = first_page_items

    while current_page_items:
        if max_items is not None and len(items) >= max_items:
            break
        if reported_total_count is not None and page_no * api_page_size >= reported_total_count:
            break
        if len(current_page_items) < api_page_size:
            break

        page_no += 1
        page_params = {**first_page_params, "pageNo": page_no}
        response = requests.get(
            f"{operation_base_url}/{operation}",
            params=page_params,
            headers={"Accept": "application/json"},
            timeout=25,
        )
        response.raise_for_status()
        payload = response.json()
        current_page_items = _extract_items(payload)
        page_total_count = _extract_total_count(payload)
        if page_total_count is not None:
            reported_total_count = page_total_count
        items.extend(current_page_items)
        if max_items is not None:
            items = items[:max_items]
        page_count += 1

    return items, {
        "page_count": page_count,
        "reported_total_count": reported_total_count,
        "received_count": len(items),
    }


def _fetch_latest_preview_window(
    *,
    operation_base_url: str,
    operation: str,
    first_page_params: dict[str, str | int],
    first_page_items: list[dict[str, Any]],
    reported_total_count: int | None,
    result_limit: int,
) -> tuple[list[dict[str, Any]], dict[str, int | None]]:
    """Read the newest source page without paging through a full test query.

    The public bid-notice lists are returned from older to newer registration
    timestamps.  A test preview therefore has to read the final page, rather
    than truncating the first page, before the application sorts by published
    time.
    """

    api_page_size = int(first_page_params["numOfRows"])
    if not reported_total_count or reported_total_count <= api_page_size:
        return first_page_items, {
            "page_count": 1,
            "reported_total_count": reported_total_count,
            "received_count": len(first_page_items),
        }

    last_page_no = (reported_total_count + api_page_size - 1) // api_page_size
    response = requests.get(
        f"{operation_base_url}/{operation}",
        params={**first_page_params, "pageNo": last_page_no},
        headers={"Accept": "application/json"},
        timeout=25,
    )
    response.raise_for_status()
    last_page_items = _extract_items(response.json())

    # A partially filled final page can contain fewer notices than the test
    # limit. Add only the immediately preceding page in that case.
    latest_items = list(last_page_items)
    page_count = 2
    if len(latest_items) < result_limit and last_page_no > 1:
        previous_response = requests.get(
            f"{operation_base_url}/{operation}",
            params={**first_page_params, "pageNo": last_page_no - 1},
            headers={"Accept": "application/json"},
            timeout=25,
        )
        previous_response.raise_for_status()
        latest_items = _extract_items(previous_response.json()) + latest_items
        page_count += 1

    # The final source page itself is not guaranteed to be ordered by
    # bidNtceDt. Return its complete window and let preview_bid_notices apply
    # the explicit published-at sort before it keeps the final test limit.
    return latest_items, {
        "page_count": page_count,
        "reported_total_count": reported_total_count,
        "received_count": len(latest_items),
    }


def _list_operations(settings: PersonalCollectionSettings) -> list[str]:
    operations: list[str] = []
    domestic_selected = not settings.procurement_types or "내자" in settings.procurement_types
    if domestic_selected:
        # 민간물품은 누리장터 별도 API이므로 명시적으로 선택했을 때만 조회한다.
        work_types = settings.work_types or list(WORK_TYPE_OPERATIONS)
        for work_type in work_types:
            operation = (
                PRIVATE_GOODS_OPERATION
                if work_type == PRIVATE_GOODS_WORK_TYPE
                else WORK_TYPE_OPERATIONS.get(work_type)
            )
            if operation and operation not in operations:
                operations.append(operation)
    if "외자" in settings.procurement_types:
        operations.append(FOREIGN_OPERATION)
    return operations


def _fetch_bid_notices(
    settings: PersonalCollectionSettings,
    *,
    max_items_per_query: int | None = None,
) -> list[dict[str, Any]]:
    service_key = _normalize_service_key(os.getenv("G2B_SERVICE_KEY", ""))
    if not service_key:
        raise ValueError("G2B_SERVICE_KEY가 없습니다. 로컬 환경 설정에서 공공데이터포털 인증키를 확인해주세요.")

    now = datetime.now(KST)
    lookback_days = max(1, int(os.getenv("G2B_LOOKBACK_DAYS", "14")))
    default_query_start = now - timedelta(days=lookback_days)
    query_start = datetime.combine(settings.posted_date_start or default_query_start.date(), time.min, tzinfo=KST)
    requested_query_end = datetime.combine(settings.posted_date_end or now.date(), time(23, 59), tzinfo=KST)
    query_end = min(requested_query_end, now)
    if query_start > query_end:
        raise ValueError("게시일자 시작일은 종료일보다 늦을 수 없습니다.")
    title_terms = [keyword.strip() for keyword in settings.required_title_keywords if keyword.strip()] or [None]
    base_url = os.getenv("G2B_API_BASE_URL", G2B_API_BASE_URL).rstrip("/")
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    source_notice_ids: set[str] = set()
    upstream_queries: list[dict[str, Any]] = []

    for operation in _list_operations(settings):
        for title_term in title_terms:
            params: dict[str, str | int] = {
                "serviceKey": service_key,
                "type": "json",
                # Keep one full source page even in test mode. The endpoint
                # sorts oldest first, so its totalCount is needed to request
                # the final, newest page.
                "numOfRows": 100,
                "pageNo": 1,
                "inqryDiv": "1",
                "inqryBgnDt": query_start.strftime("%Y%m%d%H%M"),
                "inqryEndDt": query_end.strftime("%Y%m%d%H%M"),
            }
            # This search endpoint supports bidNtceNm. The final AND comparison
            # still runs only against the returned bidNtceNm field below.
            if title_term:
                params["bidNtceNm"] = title_term
            try:
                operation_base_url = (
                    PRIVATE_G2B_API_BASE_URL
                    if operation == PRIVATE_GOODS_OPERATION
                    else base_url
                )
                response = requests.get(
                    f"{operation_base_url}/{operation}",
                    params=params,
                    headers={"Accept": "application/json"},
                    timeout=25,
                )
                response.raise_for_status()
                response_payload = response.json()
                first_page_items = _extract_items(response_payload)
                reported_total_count = _extract_total_count(response_payload)
                if max_items_per_query is not None:
                    items, query_trace = _fetch_latest_preview_window(
                        operation_base_url=operation_base_url,
                        operation=operation,
                        first_page_params=params,
                        first_page_items=first_page_items,
                        reported_total_count=reported_total_count,
                        result_limit=max_items_per_query,
                    )
                else:
                    items, query_trace = _fetch_remaining_pages(
                        operation_base_url=operation_base_url,
                        operation=operation,
                        first_page_params=params,
                        first_page_items=first_page_items,
                        reported_total_count=reported_total_count,
                    )
                upstream_queries.append(
                    {
                        "operation": operation,
                        "title_term": title_term,
                        **query_trace,
                    }
                )
            except requests.HTTPError as error:
                status_code = error.response.status_code if error.response is not None else "unknown"
                raise ValueError(f"나라장터 API 호출에 실패했습니다 (HTTP {status_code}). 인증키와 요청 상태를 확인해주세요.") from error
            except requests.RequestException as error:
                raise ValueError("나라장터 API와 통신하지 못했습니다. 잠시 후 다시 시도해주세요.") from error
            except ValueError:
                raise
            except Exception as error:
                raise ValueError("나라장터 API 응답을 해석하지 못했습니다.") from error

            for item in items:
                item = {
                    **item,
                    "_collection_work_type": OPERATION_WORK_TYPE_LABELS.get(operation),
                }
                common_record = map_bid_notice_api_item(item)
                if common_record.bid_notice_no:
                    source_notice_ids.add(
                        f"{common_record.bid_notice_no}|{common_record.bid_notice_ord or ''}"
                    )
                common_key = bid_notice_dedup_key(common_record.bid_notice_no, common_record.bid_notice_ord)
                fallback_key = "|".join(
                    [
                        operation,
                        _clean_text(item.get("bidNtceNm")) or "",
                        _clean_text(item.get("dminsttNm")) or "",
                    ]
                )
                key = f"bid|{common_key[0]}|{common_key[1]}" if common_key else f"preview|{fallback_key}"
                if key not in seen:
                    seen.add(key)
                    rows.append(item)
    append_query_history(
        settings=settings,
        upstream_queries=upstream_queries,
        source_notice_ids=source_notice_ids,
    )
    return rows


def _direct_work_type(item: dict[str, Any]) -> str | None:
    raw_value = _clean_text(item.get("workType") or item.get("workTypeNm") or item.get("bidNtceKindNm"))
    if raw_value in {"물품", "민간물품", "일반용역", "기술용역", "용역", "공사", "외자"}:
        return raw_value
    return _clean_text(item.get("_collection_work_type"))


def _direct_procurement_type(item: dict[str, Any]) -> str | None:
    raw_value = _clean_text(item.get("procurementType") or item.get("procurementTypeNm") or item.get("intrntYn"))
    if raw_value in {"내자", "외자"}:
        return raw_value
    return None


def _direct_regions(item: dict[str, Any]) -> list[str] | None:
    value = item.get("participationRegions")
    if isinstance(value, list) and all(isinstance(region, str) for region in value):
        return value
    return None


def _direct_datetime(item: dict[str, Any], *field_names: str) -> datetime | None:
    """Read only the named raw source field; never substitute another date."""

    for field_name in field_names:
        parsed = _parse_kst_datetime(item.get(field_name))
        if parsed is not None:
            return parsed
    return None


def _direct_amount(item: dict[str, Any], *field_names: str) -> int | None:
    """Read an amount only from an explicitly named source field."""

    for field_name in field_names:
        parsed = _parse_amount(item.get(field_name))
        if parsed is not None:
            return parsed
    return None


def _direct_text(item: dict[str, Any], *field_names: str) -> str | None:
    for field_name in field_names:
        value = _clean_text(item.get(field_name))
        if value is not None:
            return value
    return None


def _business_amount(item: dict[str, Any]) -> int | None:
    """Return only a source-labelled business amount or its explicit components."""

    direct_amount = _direct_amount(item, "businessAmount", "business_amount", "bsnsAmount")
    if direct_amount is not None:
        return direct_amount

    # 나라장터 가격 영역은 사업금액을 '추정가격 + 부가세'로 표시한다.
    estimated_price = _direct_amount(item, "presmptPrce")
    vat = _direct_amount(item, "VAT", "vat")
    if estimated_price is not None and vat is not None:
        return estimated_price + vat
    return None


def _candidate_from_item(item: dict[str, Any]) -> tuple[BidNoticeCandidate, object]:
    common_record = map_bid_notice_api_item(item)
    candidate = BidNoticeCandidate(
        bid_notice_no=common_record.bid_notice_no,
        bid_notice_ord=common_record.bid_notice_ord,
        bid_ntce_nm=common_record.business_name,
        demand_agency_name=common_record.demand_agency_name,
        demand_agency_code=_clean_text(item.get("dminsttCd") or item.get("dminsttCdNm")),
        work_type=_direct_work_type(item),
        procurement_type=_direct_procurement_type(item),
        base_amount=common_record.base_amount,
        participation_regions=_direct_regions(item),
        proposal_deadline=common_record.proposal_deadline,
        published_at=_direct_datetime(item, "bidNtceDt", "bidNtceRegDt"),
        bid_closing_at=_direct_datetime(item, "bidClseDt"),
        progress_status=_direct_text(item, "bidNtceSttusNm", "bidNtceSttus"),
        detail_procedure=_direct_text(item, "prssBsneUntNm"),
        detail_procedure_status=_direct_text(item, "bsnePrssPrgrsSeNm"),
        business_amount=_business_amount(item),
        source_url=_direct_text(item, "bidNtceDtlUrl", "bidNtceUrl"),
        detail_enrichment_status="DETAIL_REQUIRED" if any(
            value is None
            for value in [common_record.base_amount, common_record.proposal_deadline, _direct_regions(item)]
        ) else "LIST_ONLY",
    )
    return candidate, common_record


def preview_bid_notices(payload: BidNoticePreviewRequest) -> BidNoticePreviewResponse:
    results: list[BidNoticePreviewItem] = []
    result_limit = payload.test_result_limit
    for item in _fetch_bid_notices(
        payload.collection_setting,
        max_items_per_query=result_limit,
    ):
        candidate, common_record = _candidate_from_item(item)
        status, decisions = classify_bid_notice(payload.collection_setting, candidate)
        key = bid_notice_dedup_key(candidate.bid_notice_no, candidate.bid_notice_ord)
        record_id = (
            f"{key[0]}-{key[1]}"
            if key
            else hashlib.sha1(
                f"{candidate.bid_ntce_nm}|{candidate.demand_agency_name}".encode("utf-8")
            ).hexdigest()[:20]
        )
        results.append(
            BidNoticePreviewItem(
                record_id=record_id,
                bid_notice_no=candidate.bid_notice_no,
                bid_notice_ord=candidate.bid_notice_ord,
                business_name=candidate.bid_ntce_nm,
                demand_agency_name=candidate.demand_agency_name,
                work_type=candidate.work_type,
                procurement_type=candidate.procurement_type,
                base_amount=candidate.base_amount,
                participation_regions=candidate.participation_regions,
                proposal_deadline=candidate.proposal_deadline,
                published_at=candidate.published_at,
                bid_closing_at=candidate.bid_closing_at,
                progress_status=candidate.progress_status,
                detail_procedure=candidate.detail_procedure,
                detail_procedure_status=candidate.detail_procedure_status,
                business_amount=candidate.business_amount,
                source_url=candidate.source_url,
                detail_enrichment_status=candidate.detail_enrichment_status,
                match_status=status,
                column_decisions=decisions,
                common_storage_record=common_record,
                attachment_sources=attachment_sources_from_notice_item(item),
            )
        )
        results[-1] = results[-1].model_copy(
            update=preview_enrichment_fields(results[-1])
        )

    results.sort(
        key=lambda row: row.published_at or datetime.min.replace(tzinfo=KST),
        reverse=True,
    )
    if result_limit is not None:
        results = results[:result_limit]

    # A preview is only useful once every displayed notice has been checked
    # against its detail page and every available attachment.  Keep this after
    # the test-mode limit so a 10-notice test performs the same complete
    # analysis as production without downloading documents for rows that will
    # not be shown.
    if results:
        results = [
            apply_industry_restriction_exclusion(item)
            for item in enrich_bid_notice_items(results)
        ]

    summary = {
        "fetched_count": len(results),
        "priority_count": sum(row.match_status == "PRIORITY" for row in results),
        "review_count": sum(row.match_status == "REVIEW" for row in results),
        "exclude_count": sum(row.match_status == "EXCLUDE" for row in results),
    }
    # The browser owns display pagination (20 rows per screen). Returning all
    # collected rows here prevents the upstream API's 100-row page size from
    # silently hiding notices after the first page.
    return BidNoticePreviewResponse(
        summary=summary,
        items=results,
        page=1,
        page_size=len(results),
        total_count=len(results),
    )
