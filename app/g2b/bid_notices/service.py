import hashlib
import json
import logging
import os
from collections.abc import Callable, Iterable
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qsl, urlparse, urlunparse
from uuid import uuid4

import requests
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.data.models import (
    ScraperConfigModel,
    ScraperNoticeModel,
    ScraperRunModel,
)
from app.g2b.bid_notice import (
    REGION_API_EMPTY,
    REGION_API_ERROR,
    REGION_API_VALUE,
    canonical_bid_notice_identity,
    clean_optional_text,
    infer_two_stage_bid,
    missing_bid_notice_context_fields,
    parse_business_amount,
    parse_g2b_datetime,
    parse_official_amount,
)
from app.g2b.keyword_policy import evaluate_keyword_title, normalize_keywords
from app.g2b.opening_results.models import BidOpeningRoundModel
from app.schemas import (
    ScraperConfig,
    ScraperDedupFilterRequest,
    ScraperDedupFilterResponse,
    ScraperNotice,
    ScraperRunReportRequest,
    ScraperRunReportResponse,
    ScraperRunSummary,
    TriggerScraperResponse,
)
from app.services.cloud_scheduler_service import (
    get_scheduler_status,
    run_scheduler_job_now,
)


logger = logging.getLogger(__name__)
DEFAULT_G2B_BID_NOTICE_SOURCE_URL = (
    "https://apis.data.go.kr/1230000/ad/BidPublicInfoService/"
    "getBidPblancListInfoServc"
)


def _parse_notify_times(raw: object) -> list[time]:
    if isinstance(raw, time):
        return [raw]

    if isinstance(raw, timedelta):
        total_seconds = int(raw.total_seconds()) % (24 * 60 * 60)
        hour = total_seconds // 3600
        minute = (total_seconds % 3600) // 60
        second = total_seconds % 60
        return [time(hour=hour, minute=minute, second=second)]

    if isinstance(raw, list):
        chunks = [str(item).strip() for item in raw]
    else:
        chunks = [item.strip() for item in str(raw or "").split(",")]

    parsed: list[time] = []
    for candidate in chunks:
        if not candidate:
            continue
        try:
            parsed.append(time.fromisoformat(candidate))
        except ValueError:
            continue

    if not parsed:
        parsed = [time(hour=9, minute=0)]

    unique: dict[str, time] = {}
    for item in parsed:
        unique[item.isoformat()] = item
    return [unique[key] for key in sorted(unique.keys())]


def _serialize_notify_times(values: list[time]) -> str:
    unique: dict[str, time] = {}
    for item in values:
        unique[item.isoformat()] = item
    if not unique:
        unique["09:00:00"] = time(hour=9, minute=0)
    return ",".join(sorted(unique.keys()))


def get_scraper_config(db: Session) -> ScraperConfig:
    row = db.execute(select(ScraperConfigModel).limit(1)).scalar_one()

    emails = [item.strip() for item in row.receiver_emails.split(",") if item.strip()]
    keywords = normalize_keywords(row.keywords)
    excluded_keywords = normalize_keywords(row.excluded_keywords)
    gsheet_ids = [item.strip() for item in (row.gsheet_ids or "").split(",") if item.strip()]

    config = ScraperConfig(
        enabled=row.enabled,
        notify_times=_parse_notify_times(row.notify_times),
        gsheet_ids=gsheet_ids,
        receiver_emails=emails,
        keywords=keywords,
        excluded_keywords=excluded_keywords,
        recent_runs=list_scraper_runs(db, limit=10),
    )
    config.scheduler_status = get_scheduler_status(config)
    return config


def upsert_scraper_config(db: Session, config: ScraperConfig) -> ScraperConfig:
    result = db.execute(select(ScraperConfigModel).limit(1))
    row = result.scalar_one_or_none()
    if row is None:
        # 데이터가 없는 경우: 새 객체 생성(Insert) 로직
        serialized_notify_times = _serialize_notify_times(config.notify_times)
        row = ScraperConfigModel(
            enabled=config.enabled,
            notify_times=serialized_notify_times,
            gsheet_ids=",".join(item.strip() for item in config.gsheet_ids if item.strip()),
            receiver_emails=",".join(str(email) for email in config.receiver_emails),
            keywords=",".join(normalize_keywords(config.keywords)),
            excluded_keywords=",".join(normalize_keywords(config.excluded_keywords)),
            updated_at=datetime.now(timezone.utc)
        )
        db.add(row)
    else:
        # 데이터가 있는 경우: 기존 객체 수정(Update) 로직
        row.enabled = config.enabled
        serialized_notify_times = _serialize_notify_times(config.notify_times)
        try:
            row.notify_times = serialized_notify_times
            db.flush()
        except Exception:
            db.rollback()
            row.enabled = config.enabled
            try:
                # Legacy DB compatibility: notify_times가 TIME 타입이면 TEXT로 승격 후 재시도
                db.execute(text("ALTER TABLE scraper_configs MODIFY COLUMN notify_times TEXT NOT NULL"))
                db.flush()
                row.notify_times = serialized_notify_times
                db.flush()
            except Exception:
                # ALTER 권한이 없거나 실패하면 최소한 첫 번째 시각이라도 저장
                db.rollback()
                row.enabled = config.enabled
                row.notify_times = _parse_notify_times(serialized_notify_times)[0]
        row.gsheet_ids = ",".join(item.strip() for item in config.gsheet_ids if item.strip())
        row.receiver_emails = ",".join(str(email) for email in config.receiver_emails)
        row.keywords = ",".join(normalize_keywords(config.keywords))
        row.excluded_keywords = ",".join(normalize_keywords(config.excluded_keywords))
        row.updated_at = datetime.now(timezone.utc)
    db.commit()
    return get_scraper_config(db)


def create_scraper_task(config: ScraperConfig, reason: str | None) -> TriggerScraperResponse:
    reason_text = reason or "manual"
    scheduler_run = run_scheduler_job_now(config, reason)
    if scheduler_run is not None:
        return TriggerScraperResponse(
            accepted=True,
            message=(
                "Cloud Scheduler 수동 실행이 요청되었습니다. "
                f"job={scheduler_run['job_name']}, reason={reason_text}"
            ),
            task_id=scheduler_run["job_name"],
        )

    task_id = str(uuid4())
    message = (
        "Scraper 실행 요청이 등록되었습니다. "
        f"notify_times={len(config.notify_times)}개, receivers={len(config.receiver_emails)}명, reason={reason_text}"
    )
    return TriggerScraperResponse(accepted=True, message=message, task_id=task_id)


def _parse_deadline(raw: str) -> datetime | None:
    return parse_g2b_datetime(raw)


def _fetch_g2b_notices(
    keywords: list[str],
    excluded_keywords: list[str] | None = None,
) -> list[ScraperNotice]:
    source_url = settings.scraper_private_api_base.strip()
    if not source_url:
        return []

    notices: list[ScraperNotice] = []
    timeout = 20
    for keyword in keywords:
        try:
            response = requests.get(
                source_url,
                params={"keyword": keyword},
                headers={"Accept": "application/json"},
                timeout=timeout,
            )
            response.raise_for_status()
            payload = response.json()
        except Exception:
            continue

        items: list[dict] = []
        if isinstance(payload, list):
            items = [item for item in payload if isinstance(item, dict)]
        elif isinstance(payload, dict):
            for key in ("items", "results", "data"):
                if isinstance(payload.get(key), list):
                    items = [item for item in payload[key] if isinstance(item, dict)]
                    break

        for item in items:
            title = str(
                item.get("title")
                or item.get("noticeTitle")
                or item.get("bidNtceNm")
                or ""
            ).strip()
            if not title:
                continue
            decision = evaluate_keyword_title(title, keywords, excluded_keywords)
            if not decision.keep:
                continue
            notices.append(
                ScraperNotice(
                    notice_id=str(item.get("notice_id") or item.get("noticeId") or item.get("bidNtceNo") or "").strip(),
                    title=title,
                    agency=str(item.get("agency") or item.get("organization") or item.get("ntceInsttNm") or "").strip(),
                    estimated_price=str(item.get("estimated_price") or item.get("estPrice") or item.get("presmptPrce") or "").strip(),
                    published_at=_parse_deadline(
                        str(
                            item.get("published_at")
                            or item.get("created_at")
                            or item.get("rgstDt")
                            or item.get("bidNtceDt")
                            or ""
                        )
                    ),
                    deadline_at=_parse_deadline(
                        str(item.get("deadline_at") or item.get("deadline") or item.get("bidClseDt") or "")
                    ),
                    notice_url=str(
                        item.get("notice_url")
                        or item.get("url")
                        or item.get("link")
                        or item.get("bidNtceDtlUrl")
                        or ""
                    ).strip(),
                    bid_notice_no=clean_optional_text(
                        item.get("bid_notice_no") or item.get("bidNtceNo")
                    ),
                    bid_notice_ord=clean_optional_text(
                        item.get("bid_notice_ord") or item.get("bidNtceOrd")
                    ),
                    business_name=clean_optional_text(
                        item.get("business_name") or item.get("bidNtceNm")
                    ),
                    demand_agency_name=clean_optional_text(
                        item.get("demand_agency_name") or item.get("dminsttNm")
                    ),
                    # DB 필드명은 호환성상 base_amount지만 값은 사업금액이다.
                    base_amount=parse_business_amount(item),
                    prearranged_price_decision_method=clean_optional_text(
                        item.get("prearranged_price_decision_method")
                        or item.get("prearngPrceDcsnMthdNm")
                    ),
                    proposal_deadline=parse_g2b_datetime(
                        item.get("proposal_deadline") or item.get("bidClseDt")
                    ),
                    region_restriction=clean_optional_text(
                        item.get("region_restriction")
                        or item.get("prtcptPsblRgnNm")
                    ),
                    region_restriction_api_status=(
                        clean_optional_text(
                            item.get("region_restriction_api_status")
                        )
                        or (
                            REGION_API_VALUE
                            if clean_optional_text(
                                item.get("region_restriction")
                                or item.get("prtcptPsblRgnNm")
                            )
                            else None
                        )
                    ),
                    is_two_stage_bid=infer_two_stage_bid(
                        item.get("is_two_stage_bid"),
                        item.get("bidMethdNm"),
                        item.get("cntrctCnclsMthdNm"),
                        item.get("sucsfbidMthdNm"),
                    ),
                )
            )
    return notices


def _build_sheets_service():
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
    except Exception:
        return None

    inline_json = ""
    if inline_json:
        account = json.loads(inline_json)
        creds = service_account.Credentials.from_service_account_info(
            account,
            scopes=["https://www.googleapis.com/auth/spreadsheets"],
        )
        return build("sheets", "v4", credentials=creds)

    try:
        return build("sheets", "v4")
    except Exception:
        return None


def _append_notices_to_sheet(config: ScraperConfig, run_id: str, notices: list[ScraperNotice]) -> int:
    sheet_ids = [item.strip() for item in config.gsheet_ids if item.strip()]
    fallback = settings.gsheet_id.strip()
    if not sheet_ids and fallback:
        sheet_ids = [fallback]
    tab_name = settings.gsheet_tab_name.strip() or "나라장터 공고 수집 목록"
    if not sheet_ids or not notices:
        return 0

    service = _build_sheets_service()
    if service is None:
        return 0

    values: list[list[str]] = []
    now_iso = datetime.now(timezone.utc).isoformat()
    for notice in notices:
        values.append(
            [
                now_iso,
                run_id,
                notice.notice_id,
                notice.title,
                notice.agency,
                notice.estimated_price,
                notice.deadline_at.isoformat() if notice.deadline_at else "",
                notice.notice_url,
            ]
        )

    success_count = 0
    for sheet_id in sheet_ids:
        try:
            service.spreadsheets().values().append(
                spreadsheetId=sheet_id,
                range=f"{tab_name}!A:H",
                valueInputOption="RAW",
                insertDataOption="INSERT_ROWS",
                body={"values": values},
            ).execute()
            success_count += len(values)
        except Exception:
            continue

    return success_count


def _trigger_apps_script_mail_webhook(config: ScraperConfig, run_id: str, notices: list[ScraperNotice]) -> bool:
    webhook_url = settings.apps_script_webhook_url.strip()
    if not webhook_url or not notices:
        return False
    try:
        response = requests.post(
            webhook_url,
            timeout=20,
            json={
                "run_id": run_id,
                "receiver_emails": [str(email) for email in config.receiver_emails],
                "sheet_ids": config.gsheet_ids or [settings.gsheet_id],
                "sheet_tab_name": settings.gsheet_tab_name,
                "notice_count": len(notices),
            },
        )
        return 200 <= response.status_code < 300
    except Exception:
        return False


def run_scraper_pipeline(
    db: Session,
    config: ScraperConfig,
    reason: str | None,
) -> TriggerScraperResponse:
    if not config.enabled:
        return TriggerScraperResponse(
            accepted=True,
            message="스크래퍼가 비활성 상태라 실행이 건너뛰어졌습니다.",
            task_id="disabled",
        )

    run_id = str(uuid4())
    notices = _fetch_g2b_notices(config.keywords, config.excluded_keywords)
    notice_count = len(notices)
    filtered = filter_new_scraper_notices(
        db,
        ScraperDedupFilterRequest(
            run_id=run_id,
            notices=notices,
        ),
    )
    deduped_count = filtered.filtered_count
    kept_notices = filtered.notices
    sheet_written_count = _append_notices_to_sheet(config, run_id, kept_notices)
    mail_triggered = _trigger_apps_script_mail_webhook(config, run_id, kept_notices)

    status = "success"
    error_message = None
    incomplete_notice_count = sum(
        1 for notice in notices if missing_bid_notice_context_fields(notice)
    )
    if kept_notices and sheet_written_count == 0:
        status = "partial"
        error_message = "Google Sheet 기록 실패"

    if kept_notices and not mail_triggered:
        status = "partial" if status == "success" else status
        if error_message:
            error_message += ", Apps Script 메일 트리거 실패"
        else:
            error_message = "Apps Script 메일 트리거 실패"

    if incomplete_notice_count:
        status = "failed"
        error_message = (
            f"공식 필드가 미완성인 입찰공고가 {incomplete_notice_count}건 있어 "
            "수집 체크포인트를 갱신하지 않았습니다."
        )

    record_scraper_run_report(
        db,
        ScraperRunReportRequest(
            run_id=run_id,
            source="api_server",
            status=status,
            keyword_count=len(config.keywords),
            notice_count=notice_count,
            deduped_count=deduped_count,
            email_sent_count=1 if mail_triggered else 0,
            sheet_written_count=sheet_written_count,
            error_message=error_message,
            executed_at=datetime.now(timezone.utc),
            notices=kept_notices,
        ),
    )

    return TriggerScraperResponse(
        accepted=True,
        message=(
            f"스크래퍼 실행 완료: status={status}, notices={notice_count}, "
            f"deduped={deduped_count}, sheet={sheet_written_count}, reason={reason or 'manual'}"
        ),
        task_id=run_id,
    )


def _to_run_summary(row: ScraperRunModel) -> ScraperRunSummary:
    return ScraperRunSummary(
        run_id=row.run_id,
        status=row.status,
        keyword_count=row.keyword_count,
        notice_count=row.notice_count,
        deduped_count=row.deduped_count,
        email_sent_count=row.email_sent_count,
        sheet_written_count=row.sheet_written_count,
        error_message=row.error_message,
        executed_at=row.executed_at,
    )


def list_scraper_runs(db: Session, limit: int = 20) -> list[ScraperRunSummary]:
    safe_limit = max(1, min(limit, 100))
    rows = (
        db.execute(
            select(ScraperRunModel)
            .order_by(ScraperRunModel.executed_at.desc())
            .limit(safe_limit)
        )
        .scalars()
        .all()
    )
    return [_to_run_summary(row) for row in rows]


def _make_legacy_dedup_key(notice: ScraperNotice) -> str:
    notice_id = (notice.notice_id or "").strip().lower()
    title = (notice.title or "").strip().lower()
    raw = notice_id or title
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _make_dedup_key(notice: ScraperNotice) -> str:
    official_identity = canonical_bid_notice_identity(
        notice.bid_notice_no,
        notice.bid_notice_ord,
    )
    if official_identity is not None:
        raw = "bid-notice:" + "|".join(official_identity)
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()
    return _make_legacy_dedup_key(notice)


def _notice_fields_for_db(notice: ScraperNotice) -> dict[str, object | None]:
    """DB 컬럼 길이에 맞춤. Pydantic 스키마에 max_length가 없는 필드가 길면 commit 시 DB 오류가 난다."""
    bid_notice_no = clean_optional_text(notice.bid_notice_no, max_length=160)
    bid_notice_ord = clean_optional_text(notice.bid_notice_ord, max_length=20)
    if bid_notice_no and not bid_notice_ord:
        bid_notice_ord = "00"
    return {
        "notice_id": (notice.notice_id or "")[:160],
        "title": (notice.title or "")[:500],
        "agency": ((notice.agency or "")[:240] or None),
        "estimated_price": ((notice.estimated_price or "")[:120] or None),
        "notice_url": ((notice.notice_url or "")[:600] or None),
        "published_at": notice.published_at,
        "deadline_at": notice.deadline_at,
        "bid_notice_no": bid_notice_no,
        "bid_notice_ord": bid_notice_ord,
        "business_name": clean_optional_text(notice.business_name, max_length=500),
        "demand_agency_name": clean_optional_text(
            notice.demand_agency_name,
            max_length=240,
        ),
        "base_amount": notice.base_amount,
        "prearranged_price_decision_method": clean_optional_text(
            notice.prearranged_price_decision_method,
            max_length=120,
        ),
        "proposal_deadline": notice.proposal_deadline,
        "region_restriction": clean_optional_text(
            notice.region_restriction,
            max_length=240,
        ),
        "region_restriction_api_status": notice.region_restriction_api_status,
        "is_two_stage_bid": notice.is_two_stage_bid,
    }


def _apply_notice_fields(
    row: ScraperNoticeModel,
    fields: dict[str, object | None],
) -> None:
    for field_name, value in fields.items():
        current_value = getattr(row, field_name, None)
        if value is None and current_value is not None:
            continue
        if (
            isinstance(value, str)
            and not value.strip()
            and isinstance(current_value, str)
            and current_value.strip()
        ):
            continue
        setattr(row, field_name, value)
    if fields.get("region_restriction_api_status") == REGION_API_EMPTY:
        row.region_restriction = None


def _find_existing_scraper_notice(
    db: Session,
    notice: ScraperNotice,
    dedup_key: str,
) -> ScraperNoticeModel | None:
    existing = db.execute(
        select(ScraperNoticeModel).where(ScraperNoticeModel.dedup_key == dedup_key)
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    if canonical_bid_notice_identity(notice.bid_notice_no, notice.bid_notice_ord) is None:
        return None
    legacy_key = _make_legacy_dedup_key(notice)
    if legacy_key == dedup_key:
        return None
    legacy = db.execute(
        select(ScraperNoticeModel).where(
            ScraperNoticeModel.dedup_key == legacy_key,
            ScraperNoticeModel.bid_notice_no.is_(None),
        )
    ).scalar_one_or_none()
    if legacy is not None:
        legacy.dedup_key = dedup_key
    return legacy


def _resolve_bid_notice_operation_url(
    operation_name: str,
    *,
    env_name: str | None = None,
) -> str:
    if env_name:
        explicit_url = os.getenv(env_name, "").strip()
        if explicit_url:
            return explicit_url

    source_url = os.getenv("G2B_SOURCE_URL", "").strip()
    if not source_url:
        source_url = DEFAULT_G2B_BID_NOTICE_SOURCE_URL
    parsed = urlparse(source_url)
    path = parsed.path.rstrip("/")
    if path.lower().endswith("bidpublicinfoservice"):
        operation_path = f"{path}/{operation_name}"
    elif "/" in path:
        operation_path = f"{path.rsplit('/', 1)[0]}/{operation_name}"
    else:
        operation_path = f"/{operation_name}"
    return urlunparse(parsed._replace(path=operation_path))


def _fetch_bid_notice_api_items(
    *,
    url: str,
    params: dict[str, str],
    source_label: str,
) -> tuple[bool, list[dict[str, Any]]]:
    existing_query_keys = {
        key.lower()
        for key, _ in parse_qsl(urlparse(url).query, keep_blank_values=True)
    }
    request_params = dict(params)
    if "type" not in existing_query_keys and "_type" not in existing_query_keys:
        request_params["type"] = "json"
    if "servicekey" not in existing_query_keys:
        service_key = (
            os.getenv("G2B_SERVICE_KEY", "").strip()
            or os.getenv("G2B_AWARD_SERVICE_KEY", "").strip()
        )
        if not service_key:
            logger.warning("G2B API key is empty; skipping %s", source_label)
            return False, []
        request_params["serviceKey"] = service_key

    try:
        response = requests.get(
            url,
            params=request_params,
            headers={"Accept": "application/json"},
            timeout=max(1, int(os.getenv("G2B_HTTP_TIMEOUT_SECONDS", "20"))),
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, TypeError, ValueError):
        logger.exception("%s fetch failed", source_label)
        return False, []

    if not isinstance(payload, dict):
        return False, []
    response_payload = payload.get("response", payload)
    if not isinstance(response_payload, dict):
        return False, []
    header = response_payload.get("header")
    body = response_payload.get("body")
    if not isinstance(header, dict) or not isinstance(body, dict):
        return False, []
    result_code = str(header.get("resultCode") or "").strip()
    if result_code == "06":
        return True, []
    if result_code != "00":
        logger.warning(
            "%s API returned resultCode=%s resultMsg=%r",
            source_label,
            result_code,
            str(header.get("resultMsg") or "").strip(),
        )
        return False, []

    items: Any = body.get("items")
    if isinstance(items, dict):
        items = items.get("item")
    if items is None:
        return True, []
    if isinstance(items, dict):
        return True, [items]
    if isinstance(items, list):
        return True, [item for item in items if isinstance(item, dict)]
    return False, []


def _matching_bid_notice_items(
    items: list[dict[str, Any]],
    identity: tuple[str, str],
) -> list[dict[str, Any]]:
    return [
        item
        for item in items
        if canonical_bid_notice_identity(
            item.get("bidNtceNo") or item.get("bid_notice_no"),
            item.get("bidNtceOrd") or item.get("bid_notice_ord"),
        )
        == identity
    ]


def _decimal_text(value: Decimal | None) -> str:
    if value is None:
        return ""
    return format(value, "f")


def _fetch_official_bid_notice_context(
    round_row: BidOpeningRoundModel,
) -> ScraperNotice | None:
    if round_row.business_type != "SERVICE":
        return None
    identity = canonical_bid_notice_identity(
        round_row.bid_notice_no,
        round_row.bid_notice_ord,
    )
    if identity is None:
        return None
    notice_no, _ = identity
    notice_ord = (round_row.bid_notice_ord or "00").strip() or "00"
    common_params = {
        "pageNo": "1",
        "numOfRows": "100",
        "inqryDiv": "2",
        "bidNtceNo": notice_no,
    }

    source_url = _resolve_bid_notice_operation_url("getBidPblancListInfoServc")
    succeeded, items = _fetch_bid_notice_api_items(
        url=source_url,
        params=common_params,
        source_label=f"G2B notice context {notice_no}|{notice_ord}",
    )
    if not succeeded:
        return None
    matching_items = _matching_bid_notice_items(items, identity)
    if not matching_items:
        return None
    item = matching_items[0]

    price_method = clean_optional_text(item.get("prearngPrceDcsnMthdNm"))
    business_amount = parse_business_amount(item)

    region_restriction = clean_optional_text(
        item.get("prtcptPsblRgnNm"),
        max_length=240,
    )
    region_url = _resolve_bid_notice_operation_url(
        "getBidPblancListInfoPrtcptPsblRgn",
        env_name="G2B_REGION_SOURCE_URL",
    )
    region_succeeded, region_items = _fetch_bid_notice_api_items(
        url=region_url,
        params={**common_params, "bidNtceOrd": notice_ord},
        source_label=f"G2B participant region {notice_no}|{notice_ord}",
    )
    if region_succeeded:
        matching_region_items = _matching_bid_notice_items(region_items, identity)
        if region_items and not matching_region_items:
            region_restriction = None
            region_api_status = REGION_API_ERROR
        else:
            region_names: list[str] = []
            for region_item in matching_region_items:
                region_name = clean_optional_text(
                    region_item.get("prtcptPsblRgnNm")
                )
                if region_name and region_name not in region_names:
                    region_names.append(region_name)
            region_restriction = clean_optional_text(
                ", ".join(region_names),
                max_length=240,
            )
            region_api_status = (
                REGION_API_VALUE if region_restriction else REGION_API_EMPTY
            )
    else:
        region_api_status = REGION_API_ERROR

    estimated_price = parse_official_amount(item.get("presmptPrce"))
    proposal_deadline = parse_g2b_datetime(item.get("bidClseDt"))
    return ScraperNotice(
        notice_id=clean_optional_text(item.get("bidNtceNo")) or notice_no,
        title=(
            clean_optional_text(item.get("bidNtceNm"))
            or round_row.title
            or notice_no
        ),
        agency=(
            clean_optional_text(item.get("ntceInsttNm"))
            or clean_optional_text(item.get("dminsttNm"))
            or ""
        ),
        estimated_price=_decimal_text(estimated_price),
        published_at=parse_g2b_datetime(item.get("bidNtceDt")),
        deadline_at=proposal_deadline,
        notice_url=clean_optional_text(item.get("bidNtceDtlUrl")) or "",
        bid_notice_no=clean_optional_text(item.get("bidNtceNo")) or notice_no,
        bid_notice_ord=(
            clean_optional_text(item.get("bidNtceOrd")) or notice_ord
        ),
        business_name=(
            clean_optional_text(item.get("bidNtceNm"))
            or round_row.title
            or notice_no
        ),
        demand_agency_name=(
            clean_optional_text(item.get("dminsttNm"))
            or round_row.demand_agency_name
        ),
        # DB 필드명은 호환성상 base_amount지만 값은 사업금액이다.
        base_amount=business_amount,
        prearranged_price_decision_method=price_method,
        proposal_deadline=proposal_deadline,
        region_restriction=region_restriction,
        region_restriction_api_status=region_api_status,
        is_two_stage_bid=infer_two_stage_bid(
            None,
            item.get("bidMethdNm"),
            item.get("cntrctCnclsMthdNm"),
            item.get("sucsfbidMthdNm"),
        ),
    )


def enrich_bid_notice_contexts_for_opening_rounds(
    db: Session,
    rounds: Iterable[BidOpeningRoundModel],
    *,
    fetch_context: (
        Callable[[BidOpeningRoundModel], ScraperNotice | None] | None
    ) = None,
) -> int:
    rounds_by_key: dict[tuple[str, str], BidOpeningRoundModel] = {}
    for round_row in rounds:
        if round_row.business_type != "SERVICE":
            continue
        identity = canonical_bid_notice_identity(
            round_row.bid_notice_no,
            round_row.bid_notice_ord,
        )
        if identity is not None:
            rounds_by_key.setdefault(identity, round_row)
    if not rounds_by_key:
        return 0

    notice_numbers = {key[0] for key in rounds_by_key}
    stored_by_key: dict[tuple[str, str], list[ScraperNoticeModel]] = {}
    stored_rows = db.scalars(
        select(ScraperNoticeModel).where(
            ScraperNoticeModel.bid_notice_no.in_(notice_numbers)
        )
    )
    for stored in stored_rows:
        identity = canonical_bid_notice_identity(
            stored.bid_notice_no,
            stored.bid_notice_ord,
        )
        if identity in rounds_by_key:
            stored_by_key.setdefault(identity, []).append(stored)

    context_fetcher = fetch_context or _fetch_official_bid_notice_context
    now = datetime.now(timezone.utc)
    enriched_count = 0
    for identity, round_row in rounds_by_key.items():
        existing_rows = stored_by_key.get(identity, [])
        if len(existing_rows) > 1:
            logger.warning(
                "Skipping ambiguous bid notice context %s|%s",
                identity[0],
                identity[1],
            )
            continue
        if (
            len(existing_rows) == 1
            and existing_rows[0].last_run_id == "opening-result-context"
            and not missing_bid_notice_context_fields(existing_rows[0])
        ):
            continue
        try:
            notice = context_fetcher(round_row)
        except Exception:
            logger.exception(
                "Bid notice context enrichment failed for %s|%s",
                identity[0],
                identity[1],
            )
            continue
        if notice is None or canonical_bid_notice_identity(
            notice.bid_notice_no,
            notice.bid_notice_ord,
        ) != identity:
            continue

        dedup_key = _make_dedup_key(notice)
        stored = existing_rows[0] if existing_rows else None
        if stored is None:
            stored = _find_existing_scraper_notice(db, notice, dedup_key)
        if stored is None:
            stored = ScraperNoticeModel(
                dedup_key=dedup_key,
                first_seen_at=now,
                last_seen_at=now,
                last_run_id="opening-result-context",
            )
            db.add(stored)

        _apply_notice_fields(stored, _notice_fields_for_db(notice))
        # 과거 bssamt 값이 남지 않도록 사업금액은 NULL도 포함해 정확히 교체한다.
        stored.base_amount = notice.base_amount
        stored.last_seen_at = now
        stored.last_run_id = "opening-result-context"
        enriched_count += 1

    if enriched_count:
        db.flush()
    return enriched_count


def get_last_scraper_run_time(db: Session) -> datetime | None:
    return _last_notified_at(db)


def _last_notified_at(db: Session) -> datetime | None:
    row = db.execute(
        select(ScraperRunModel)
        .where(ScraperRunModel.status.in_(["success", "partial"]))
        .order_by(ScraperRunModel.executed_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    return row.executed_at if row is not None else None


def filter_new_scraper_notices(
    db: Session,
    payload: ScraperDedupFilterRequest,
) -> ScraperDedupFilterResponse:
    now = datetime.now(timezone.utc)
    since_notified_at = payload.since_notified_at or _last_notified_at(db)
    # offset-naive → KST(UTC+9)로 통일하여 비교 오류 방지
    kst = timezone(timedelta(hours=9))
    if since_notified_at is not None and since_notified_at.tzinfo is None:
        since_notified_at = since_notified_at.replace(tzinfo=kst)
    kept: list[ScraperNotice] = []

    for notice in payload.notices:
        published = notice.published_at
        if published is not None and published.tzinfo is None:
            published = published.replace(tzinfo=kst)
        is_stale_notice = bool(
            since_notified_at is not None
            and published is not None
            and published <= since_notified_at
        )

        dedup_key = _make_dedup_key(notice)
        existing = _find_existing_scraper_notice(db, notice, dedup_key)

        if existing is None:
            if is_stale_notice:
                continue
            fields = _notice_fields_for_db(notice)
            row = ScraperNoticeModel(
                dedup_key=dedup_key,
                first_seen_at=now,
                last_seen_at=now,
                last_run_id=payload.run_id,
            )
            _apply_notice_fields(row, fields)
            db.add(row)
            # 같은 요청 payload 안에 동일 dedup_key가 두 번 오면, flush 전에는 DB/SELECT에 안 보여
            # 두 번째 행이 또 INSERT 되며 UNIQUE(dedup_key) 위반 → 500. 반드시 flush.
            db.flush()
            kept.append(notice)
            continue

        fields = _notice_fields_for_db(notice)
        _apply_notice_fields(existing, fields)
        existing.last_seen_at = now
        existing.last_run_id = payload.run_id

    db.commit()
    input_count = len(payload.notices)
    kept_count = len(kept)
    return ScraperDedupFilterResponse(
        run_id=payload.run_id,
        input_count=input_count,
        kept_count=kept_count,
        filtered_count=input_count - kept_count,
        notices=kept,
    )


def record_scraper_run_report(db: Session, payload: ScraperRunReportRequest) -> ScraperRunReportResponse:
    executed_at = payload.executed_at
    if executed_at.tzinfo is None:
        executed_at = executed_at.replace(tzinfo=timezone.utc)

    row = db.execute(
        select(ScraperRunModel).where(ScraperRunModel.run_id == payload.run_id)
    ).scalar_one_or_none()

    if row is None:
        row = ScraperRunModel(
            run_id=payload.run_id,
            source=payload.source,
            status=payload.status,
            keyword_count=payload.keyword_count,
            notice_count=payload.notice_count,
            deduped_count=payload.deduped_count,
            email_sent_count=payload.email_sent_count,
            sheet_written_count=payload.sheet_written_count,
            error_message=payload.error_message,
            executed_at=executed_at,
        )
        db.add(row)
    else:
        row.source = payload.source
        row.status = payload.status
        row.keyword_count = payload.keyword_count
        row.notice_count = payload.notice_count
        row.deduped_count = payload.deduped_count
        row.email_sent_count = payload.email_sent_count
        row.sheet_written_count = payload.sheet_written_count
        row.error_message = payload.error_message
        row.executed_at = executed_at

    for notice in payload.notices:
        dedup_key = _make_dedup_key(notice)
        existing = _find_existing_scraper_notice(db, notice, dedup_key)
        if existing is None:
            fields = _notice_fields_for_db(notice)
            notice_row = ScraperNoticeModel(
                dedup_key=dedup_key,
                first_seen_at=executed_at,
                last_seen_at=executed_at,
                last_run_id=payload.run_id,
            )
            _apply_notice_fields(notice_row, fields)
            db.add(notice_row)
            db.flush()
        else:
            fields = _notice_fields_for_db(notice)
            _apply_notice_fields(existing, fields)
            existing.last_seen_at = executed_at
            existing.last_run_id = payload.run_id

    db.commit()
    return ScraperRunReportResponse(
        success=True,
        message="스크래퍼 실행 결과가 저장되었습니다.",
        run_id=payload.run_id,
    )
