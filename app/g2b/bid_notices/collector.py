import hashlib
import json
import re
from datetime import date, datetime, time, timezone
from decimal import Decimal
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

import requests
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.data.models import ScraperNoticeModel
from app.g2b.bid_notice import (
    REGION_API_EMPTY,
    REGION_API_VALUE,
    canonical_bid_notice_identity,
    infer_joint_supply_allowed,
    infer_two_stage_bid,
    parse_business_amount,
    parse_g2b_datetime,
    parse_official_amount,
)
from app.g2b.bid_notices.matching import get_enabled_bid_notice_keywords
from app.g2b.bid_notices.models import BidNoticeCollectionRunModel


KST = ZoneInfo("Asia/Seoul")
G2B_BID_NOTICE_API_BASE = "https://apis.data.go.kr/1230000/ad/BidPublicInfoService"
OPERATIONS = {
    "SERVICE": ("getBidPblancListInfoServcPPSSrch", "용역"),
    "GOODS": ("getBidPblancListInfoThngPPSSrch", "물품"),
    "CONSTRUCTION": ("getBidPblancListInfoCnstwkPPSSrch", "공사"),
}
MAX_PAGES_PER_KEYWORD = 5
LICENSE_LIMIT_OPERATION = "getBidPblancListInfoLicenseLimit"
INDUSTRY_API_VALUE = "API_VALUE"
INDUSTRY_API_EMPTY = "API_EMPTY"
INDUSTRY_API_ERROR = "API_ERROR"
ICORE_INDUSTRY_CODES = frozenset(
    {"9901", "3198", "0036", "1169", "1261", "1426", "1468", "9999"}
)


class BidNoticeCollectionError(RuntimeError):
    pass


def _kst_datetime(value: Any) -> datetime | None:
    return parse_g2b_datetime(value)


def _extract_items(payload: object) -> tuple[list[dict[str, Any]], int | None]:
    if not isinstance(payload, dict):
        raise BidNoticeCollectionError("나라장터 API 응답 형식이 올바르지 않습니다.")
    response = payload.get("response")
    if not isinstance(response, dict):
        raise BidNoticeCollectionError("나라장터 API 응답 본문이 없습니다.")
    header = response.get("header") or {}
    code = str(header.get("resultCode") or "") if isinstance(header, dict) else ""
    if code == "06":
        return [], 0
    if code != "00":
        message = str(header.get("resultMsg") or "알 수 없는 오류")
        raise BidNoticeCollectionError(f"나라장터 API 오류({code or 'unknown'}): {message}")
    body = response.get("body") or {}
    if not isinstance(body, dict):
        return [], None
    raw_items = body.get("items")
    if isinstance(raw_items, dict):
        raw_items = raw_items.get("item")
    if isinstance(raw_items, dict):
        items = [raw_items]
    elif isinstance(raw_items, list):
        items = [item for item in raw_items if isinstance(item, dict)]
    else:
        items = []
    total = parse_official_amount(body.get("totalCount"))
    return items, int(total) if total is not None else None


def _fetch_operation(
    *,
    operation: str,
    start_at: datetime,
    end_at: datetime,
    keyword: str,
) -> list[dict[str, Any]]:
    service_key = settings.g2b_award_service_key.strip()
    if not service_key:
        raise BidNoticeCollectionError("G2B_AWARD_SERVICE_KEY 설정이 없습니다.")
    params: dict[str, str | int] = {
        "serviceKey": service_key,
        "type": "json",
        "pageNo": 1,
        "numOfRows": 100,
        "inqryDiv": "1",
        "inqryBgnDt": start_at.strftime("%Y%m%d%H%M"),
        "inqryEndDt": end_at.strftime("%Y%m%d%H%M"),
        "bidNtceNm": keyword,
    }
    url = f"{G2B_BID_NOTICE_API_BASE}/{operation}"
    try:
        response = requests.get(url, params=params, headers={"Accept": "application/json"}, timeout=25)
        response.raise_for_status()
        first_items, total_count = _extract_items(response.json())
    except requests.RequestException as error:
        raise BidNoticeCollectionError("나라장터 API와 통신하지 못했습니다. 잠시 후 다시 시도해주세요.") from error
    except ValueError as error:
        raise BidNoticeCollectionError("나라장터 API 응답을 해석하지 못했습니다.") from error

    items = list(first_items)
    if total_count is None:
        return items
    page_count = (total_count + 99) // 100
    if page_count > MAX_PAGES_PER_KEYWORD:
        raise BidNoticeCollectionError(
            f"'{keyword}' 공고명 수집 결과가 너무 많습니다. 더 구체적인 포함 키워드를 설정하세요."
        )
    for page_no in range(2, page_count + 1):
        try:
            page_response = requests.get(
                url,
                params={**params, "pageNo": page_no},
                headers={"Accept": "application/json"},
                timeout=25,
            )
            page_response.raise_for_status()
            page_items, _ = _extract_items(page_response.json())
        except requests.RequestException as error:
            raise BidNoticeCollectionError("나라장터 API 페이지 조회에 실패했습니다.") from error
        items.extend(page_items)
    return items


def _dedup_key(notice_no: str, notice_ord: str) -> str:
    identity = canonical_bid_notice_identity(notice_no, notice_ord)
    if identity is None:
        raw = f"{notice_no}|{notice_ord}"
    else:
        raw = f"bid-notice|{identity[0]}|{identity[1]}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _optional_text(value: object, limit: int) -> str | None:
    text = str(value or "").strip()
    return text[:limit] if text else None


def fetch_industry_restriction_codes(
    *,
    notice_no: str,
    notice_ord: str,
) -> tuple[str | None, str]:
    """면허제한 API의 4자리 업종 코드를 상세보기 시점에 조회한다."""
    service_key = settings.g2b_award_service_key.strip()
    if not service_key:
        return None, INDUSTRY_API_ERROR
    try:
        response = requests.get(
            f"{G2B_BID_NOTICE_API_BASE}/{LICENSE_LIMIT_OPERATION}",
            params={
                "serviceKey": service_key,
                "type": "json",
                "pageNo": 1,
                "numOfRows": 100,
                "inqryDiv": "2",
                "bidNtceNo": notice_no,
                "bidNtceOrd": notice_ord,
            },
            headers={"Accept": "application/json"},
            timeout=25,
        )
        response.raise_for_status()
        items, _ = _extract_items(response.json())
    except (requests.RequestException, ValueError, BidNoticeCollectionError):
        return None, INDUSTRY_API_ERROR

    codes: list[str] = []
    for item in items:
        for field_name in ("lcnsLmtNm", "permsnIndstrytyList", "indstrytyCd"):
            for code in re.findall(r"(?<!\d)(\d{4})(?!\d)", str(item.get(field_name) or "")):
                if code not in codes:
                    codes.append(code)
    return (", ".join(codes) if codes else None), (INDUSTRY_API_VALUE if codes else INDUSTRY_API_EMPTY)


def matches_icore_industry_code(codes: str | None) -> bool:
    return bool(set(re.findall(r"(?<!\d)(\d{4})(?!\d)", codes or "")) & ICORE_INDUSTRY_CODES)


def _upsert_item(
    db: Session,
    item: dict[str, Any],
    work_type: str,
    now: datetime,
    *,
    skip_existing: bool = False,
) -> bool:
    notice_no = _optional_text(item.get("bidNtceNo"), 160)
    notice_ord = _optional_text(item.get("bidNtceOrd"), 20) or "00"
    if notice_no is None:
        return False
    dedup_key = _dedup_key(notice_no, notice_ord)
    row = db.execute(
        select(ScraperNoticeModel).where(ScraperNoticeModel.dedup_key == dedup_key)
    ).scalar_one_or_none()
    if row is not None and skip_existing:
        return False
    created = row is None
    if row is None:
        row = ScraperNoticeModel(
            dedup_key=dedup_key,
            first_seen_at=now,
            last_seen_at=now,
        )
        db.add(row)

    business_amount = parse_business_amount(item)
    official_base_amount = parse_official_amount(item.get("bsisAmount") or item.get("bssamt"))
    region = _optional_text(item.get("prtcptPsblRgnNm"), 240)
    row.notice_id = notice_no
    row.title = _optional_text(item.get("bidNtceNm"), 500) or notice_no
    row.agency = _optional_text(item.get("ntceInsttNm") or item.get("dminsttNm"), 240)
    row.estimated_price = _optional_text(item.get("presmptPrce"), 120)
    row.published_at = _kst_datetime(item.get("bidNtceDt") or item.get("bidNtceRegDt"))
    row.deadline_at = _kst_datetime(item.get("bidClseDt"))
    row.notice_url = _optional_text(item.get("bidNtceDtlUrl") or item.get("bidNtceUrl"), 600)
    row.bid_notice_no = notice_no
    row.bid_notice_ord = notice_ord
    row.business_name = row.title
    row.demand_agency_name = _optional_text(item.get("dminsttNm"), 240)
    if business_amount is not None:
        row.base_amount = business_amount
    row.prearranged_price_decision_method = _optional_text(item.get("prearngPrceDcsnMthdNm"), 120)
    row.region_restriction = region
    row.region_restriction_api_status = REGION_API_VALUE if region else REGION_API_EMPTY
    row.joint_supply_allowed = infer_joint_supply_allowed(
        item.get("cmmnSpldmdMethdCd"),
        item.get("cmmnSpldmdMethdNm"),
    )
    row.is_two_stage_bid = infer_two_stage_bid(
        item.get("twoStageBidYn"),
        item.get("bidMethdNm"),
        item.get("cntrctCnclsMthdNm"),
        item.get("sucsfbidMthdNm"),
    )
    row.work_type = work_type
    row.procurement_type = _optional_text(item.get("intrntYn"), 20)
    row.official_base_amount = official_base_amount
    row.source_payload = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
    row.last_seen_at = now
    row.last_run_id = "bid-notice-collector"
    return created


def collect_bid_notices(
    db: Session,
    *,
    start_date: date,
    end_date: date,
    business_types: list[str],
    keywords: list[str],
    skip_existing: bool = False,
    run_prefix: str = "manual",
    now: datetime | None = None,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
) -> dict[str, int | str]:
    normalized_keywords = [str(keyword).strip() for keyword in keywords if str(keyword).strip()]
    if not normalized_keywords:
        raise BidNoticeCollectionError("수집하려면 조건 설정에 포함 키워드를 한 개 이상 저장하세요.")
    start_at = window_start or datetime.combine(start_date, time.min, tzinfo=KST)
    end_at = window_end or datetime.combine(end_date, time(23, 59), tzinfo=KST)
    run = BidNoticeCollectionRunModel(
        run_key=f"{run_prefix}:{start_date.isoformat()}:{end_date.isoformat()}:{uuid4()}",
        window_start=start_at,
        window_end=end_at,
    )
    db.add(run)
    db.commit()
    try:
        rows: list[tuple[dict[str, Any], str]] = []
        seen: set[str] = set()
        for business_type in business_types:
            operation, label = OPERATIONS[business_type]
            for keyword in normalized_keywords:
                for item in _fetch_operation(
                    operation=operation,
                    start_at=start_at,
                    end_at=end_at,
                    keyword=keyword,
                ):
                    identity = canonical_bid_notice_identity(item.get("bidNtceNo"), item.get("bidNtceOrd"))
                    if identity is None:
                        continue
                    key = f"{identity[0]}|{identity[1]}"
                    if key not in seen:
                        seen.add(key)
                        rows.append((item, label))
        collected_at = now or datetime.now(timezone.utc)
        inserted = 0
        for item, label in rows:
            inserted += int(
                _upsert_item(
                    db,
                    item,
                    label,
                    collected_at,
                    skip_existing=skip_existing,
                )
            )
        run.fetched_count = len(rows)
        run.inserted_count = inserted
        run.updated_count = 0 if skip_existing else len(rows) - inserted
        run.status = "SUCCESS"
        run.finished_at = datetime.now(timezone.utc)
        db.commit()
        return {
            "run_key": run.run_key,
            "fetched_count": len(rows),
            "inserted_count": inserted,
            "updated_count": 0 if skip_existing else len(rows) - inserted,
        }
    except Exception as error:
        run.status = "FAILED"
        run.error_message = str(error)[:4000]
        run.finished_at = datetime.now(timezone.utc)
        db.commit()
        if isinstance(error, BidNoticeCollectionError):
            raise
        raise BidNoticeCollectionError("입찰공고 수집에 실패했습니다.") from error


def collect_scheduled_bid_notices(
    db: Session,
    *,
    now: datetime | None = None,
) -> dict[str, int | str]:
    collected_at = now or datetime.now(timezone.utc)
    keywords = get_enabled_bid_notice_keywords(db)
    if not keywords:
        return {
            "run_key": f"scheduled:{collected_at.date().isoformat()}:no-keywords",
            "fetched_count": 0,
            "inserted_count": 0,
            "updated_count": 0,
        }
    collection_date = collected_at.astimezone(KST).date()
    return collect_bid_notices(
        db,
        start_date=collection_date,
        end_date=collection_date,
        business_types=list(OPERATIONS),
        keywords=keywords,
        skip_existing=True,
        run_prefix="scheduled",
        now=collected_at,
        window_start=datetime.combine(collection_date, time.min, tzinfo=KST),
        window_end=collected_at.astimezone(KST),
    )
