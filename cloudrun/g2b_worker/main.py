import json
import os
import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qsl, urlparse
from uuid import uuid4

import requests
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from requests.exceptions import JSONDecodeError as RequestsJSONDecodeError

try:
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
except Exception:  # pragma: no cover
    service_account = None
    build = None


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
app = FastAPI(title="iCore G2B Scraper Worker", version="0.1.0")
logger = logging.getLogger("icore.g2b_worker")


class NoticeRow(BaseModel):
    notice_id: str = ""
    title: str = Field(..., min_length=1, max_length=500)
    agency: str = ""
    estimated_price: str = ""
    published_at: datetime | None = None
    deadline_at: datetime | None = None
    notice_url: str = ""


class ScraperJobPayload(BaseModel):
    enabled: bool = True
    notify_times: list[str] = Field(default_factory=lambda: ["09:00:00"])
    gsheet_id: str | None = None
    gsheet_tab_name: str = "나라장터 공고 수집 목록"
    receiver_emails: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)


def _normalize_string_list(raw: Any) -> list[str]:
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    if isinstance(raw, str):
        return [item.strip() for item in raw.split(",") if item.strip()]
    return []


def _backend_headers() -> dict[str, str]:
    token = os.getenv("SCRAPER_INTERNAL_TOKEN", "").strip()
    if not token:
        raise RuntimeError("SCRAPER_INTERNAL_TOKEN is required")
    return {
        "Content-Type": "application/json",
        "X-Scraper-Internal-Token": token,
    }


def _backend_base_url() -> str:
    base_url = os.getenv("BACKEND_INTERNAL_BASE_URL", "").strip().rstrip("/")
    if not base_url:
        raise RuntimeError("BACKEND_INTERNAL_BASE_URL is required")
    return base_url


def _parse_deadline(raw: Any) -> datetime | None:
    if not raw:
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    text = str(raw).strip()
    if text.isdigit():
        try:
            if len(text) == 12:
                return datetime.strptime(text, "%Y%m%d%H%M").replace(tzinfo=timezone.utc)
            if len(text) == 8:
                return datetime.strptime(text, "%Y%m%d").replace(tzinfo=timezone.utc)
        except Exception:
            return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed
    except Exception:
        return None


def _extract_from_item(item: dict[str, Any]) -> NoticeRow | None:
    title = str(item.get("title") or item.get("noticeTitle") or item.get("bidNtceNm") or "").strip()
    if not title:
        return None

    notice_id = str(item.get("notice_id") or item.get("noticeId") or item.get("bidNtceNo") or "").strip()
    agency = str(item.get("agency") or item.get("organization") or item.get("dminsttNm") or item.get("ntceInsttNm") or "").strip()
    estimated_price = str(item.get("estimated_price") or item.get("estPrice") or item.get("presmptPrce") or "").strip()
    notice_url = str(item.get("notice_url") or item.get("url") or item.get("link") or item.get("bidNtceDtlUrl") or "").strip()
    deadline_at = _parse_deadline(item.get("deadline_at") or item.get("deadline") or item.get("bidClseDt"))
    published_at = _parse_deadline(
        item.get("published_at") or item.get("created_at") or item.get("rgstDt") or item.get("bidNtceDt")
    )

    return NoticeRow(
        notice_id=notice_id,
        title=title,
        agency=agency,
        estimated_price=estimated_price,
        published_at=published_at,
        deadline_at=deadline_at,
        notice_url=notice_url,
    )


def _fetch_last_run_at() -> datetime | None:
    try:
        base_url = _backend_base_url()
        response = requests.get(
            f"{base_url}/api/scraper/internal/last-run",
            headers=_backend_headers(),
            timeout=10,
        )
        response.raise_for_status()
        raw = response.json().get("last_run_at")
        if raw:
            return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except Exception:
        logger.exception("Failed to fetch last run time from backend")
    return None


def _fetch_g2b_notices(keywords: list[str]) -> list[NoticeRow]:
    source_url = os.getenv("G2B_SOURCE_URL", "").strip()
    if not source_url:
        logger.warning("G2B_SOURCE_URL is not set – skipping fetch")
        return []
    if not keywords:
        logger.warning("No keywords – skipping fetch")
        return []

    parsed_url = urlparse(source_url)
    existing_query_keys = {key for key, _ in parse_qsl(parsed_url.query, keep_blank_values=True)}
    existing_query_keys_lc = {key.lower() for key in existing_query_keys}

    notices: list[NoticeRow] = []
    timeout = int(os.getenv("G2B_HTTP_TIMEOUT_SECONDS", "20"))

    # 나라장터 API는 inqryDiv + inqryBgnDt/inqryEndDt 가 필수
    # 최근 실행 이력 이후 공고만 조회하고, 이력이 없으면 1일 전부터 조회
    now = datetime.now(timezone.utc)
    last_run_at = _fetch_last_run_at()
    if last_run_at is None:
        query_start = now - timedelta(days=1)
        logger.info("No scraper run history found. Falling back to 1 day window.")
    else:
        query_start = last_run_at if last_run_at.tzinfo else last_run_at.replace(tzinfo=timezone.utc)
        if query_start > now:
            logger.warning("Last run time is in the future. Falling back to 1 day window.")
            query_start = now - timedelta(days=1)

    inqry_bgn = query_start.strftime("%Y%m%d%H%M")
    inqry_end = now.strftime("%Y%m%d%H%M")
    logger.info("G2B query window: %s ~ %s", inqry_bgn, inqry_end)

    def _normalize_items(payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, list):
            return [row for row in payload if isinstance(row, dict)]
        if not isinstance(payload, dict):
            return []

        response = payload.get("response")
        if isinstance(response, dict):
            body = response.get("body")
            if isinstance(body, dict):
                items = body.get("items")
                if isinstance(items, dict):
                    item = items.get("item")
                    if isinstance(item, list):
                        return [row for row in item if isinstance(row, dict)]
                    if isinstance(item, dict):
                        return [item]
                if isinstance(items, list):
                    return [row for row in items if isinstance(row, dict)]

        for key in ("items", "results", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return [row for row in value if isinstance(row, dict)]
        return []

    def _parse_xml_response(raw_body: str) -> dict[str, Any] | None:
        try:
            root = ET.fromstring(raw_body)
        except ET.ParseError:
            return None

        body = root.find(".//body")
        if body is None:
            return None

        total_count_text = ""
        total_count_node = body.find("totalCount")
        if total_count_node is not None and total_count_node.text:
            total_count_text = total_count_node.text.strip()

        item_nodes = body.findall(".//items/item")
        parsed_items: list[dict[str, str]] = []
        for item_node in item_nodes:
            row: dict[str, str] = {}
            for child in list(item_node):
                row[child.tag] = (child.text or "").strip()
            if row:
                parsed_items.append(row)

        return {
            "response": {
                "body": {
                    "totalCount": total_count_text,
                    "items": {"item": parsed_items},
                }
            }
        }

    def _extract_xml_tag(text: str, tag_name: str) -> str:
        matched = re.search(rf"<{tag_name}>(.*?)</{tag_name}>", text, flags=re.IGNORECASE | re.DOTALL)
        if not matched:
            return ""
        return matched.group(1).strip()

    for keyword in keywords:
        params = {
            "inqryDiv": "1",
            "inqryBgnDt": inqry_bgn,
            "inqryEndDt": inqry_end,
            "bidNtceNm": keyword,
            "numOfRows": "100",
            "pageNo": "1",
        }
        if "type" not in existing_query_keys_lc and "_type" not in existing_query_keys_lc:
            params["type"] = "json"
        if "servicekey" not in existing_query_keys_lc:
            logger.warning("G2B_SOURCE_URL does not include service key query parameter.")
            continue

        headers = {"Accept": "application/json"}

        try:
            response = requests.get(source_url, params=params, headers=headers, timeout=timeout)
            response.raise_for_status()
            raw_body = (response.text or "").strip()
            content_type = (response.headers.get("Content-Type") or "").lower()

            if not raw_body:
                logger.warning(
                    "G2B returned empty body. keyword=%r status=%s content_type=%s",
                    keyword,
                    response.status_code,
                    content_type,
                )
                continue

            try:
                payload = response.json()
            except RequestsJSONDecodeError:
                preview = raw_body[:400].replace("\n", " ").replace("\r", " ")
                if "xml" in content_type or raw_body.startswith("<"):
                    result_code = _extract_xml_tag(raw_body, "returnReasonCode")
                    result_message = _extract_xml_tag(raw_body, "returnAuthMsg")
                    xml_payload = _parse_xml_response(raw_body)
                    if xml_payload is None:
                        logger.error(
                            "G2B returned XML/non-JSON response. keyword=%r status=%s code=%r message=%r body_preview=%r",
                            keyword,
                            response.status_code,
                            result_code,
                            result_message,
                            preview,
                        )
                        continue
                    logger.info(
                        "G2B XML response parsed successfully. keyword=%r status=%s code=%r message=%r",
                        keyword,
                        response.status_code,
                        result_code,
                        result_message,
                    )
                    payload = xml_payload
                else:
                    logger.error(
                        "G2B returned non-JSON response. keyword=%r status=%s content_type=%s body_preview=%r",
                        keyword,
                        response.status_code,
                        content_type,
                        preview,
                    )
                    continue
        except Exception:
            logger.exception("G2B fetch failed for keyword=%r", keyword)
            continue

        body = payload.get("response", {}).get("body", {}) if isinstance(payload, dict) else {}
        total_count_raw = body.get("totalCount")
        try:
            total_count = int(str(total_count_raw).strip()) if total_count_raw is not None else None
        except Exception:
            total_count = None
        if total_count == 0:
            continue

        items = _normalize_items(payload)

        for item in items:
            parsed = _extract_from_item(item)
            if parsed is not None:
                notices.append(parsed)

    return notices


def _dedup_with_backend(run_id: str, payload: ScraperJobPayload, notices: list[NoticeRow]) -> list[NoticeRow]:
    base_url = _backend_base_url()
    dedup_url = f"{base_url}/api/scraper/internal/dedup"
    body = {
        "run_id": run_id,
        "notices": [notice.model_dump(mode="json") for notice in notices],
    }
    response = requests.post(dedup_url, headers=_backend_headers(), json=body, timeout=20)
    response.raise_for_status()
    filtered = response.json().get("notices", [])
    return [NoticeRow.model_validate(item) for item in filtered]


def _trigger_apps_script_mail_webhook(
    *,
    run_id: str,
    payload: ScraperJobPayload,
    notices: list[NoticeRow],
) -> bool:
    webhook_url = os.getenv("APPS_SCRIPT_WEBHOOK_URL", "").strip()
    if not webhook_url or not notices:
        return False

    try:
        response = requests.post(
            webhook_url,
            timeout=20,
            json={
                "run_id": run_id,
                "receiver_emails": payload.receiver_emails,
                "sheet_id": payload.gsheet_id or os.getenv("GSHEET_ID", ""),
                "sheet_tab_name": payload.gsheet_tab_name,
                "notice_count": len(notices),
            },
        )
        return 200 <= response.status_code < 300
    except Exception:
        return False


def _build_sheets_service():
    if build is None:
        logger.warning("google-api-python-client not available")
        return None

    inline_json = os.getenv("GSHEET_SERVICE_ACCOUNT_JSON", "").strip()
    if inline_json and service_account is not None:
        account = json.loads(inline_json)
        creds = service_account.Credentials.from_service_account_info(
            account,
            scopes=["https://www.googleapis.com/auth/spreadsheets"],
        )
        return build("sheets", "v4", credentials=creds)

    logger.info("GSHEET_SERVICE_ACCOUNT_JSON not set – using ADC (Cloud Run SA)")
    try:
        return build("sheets", "v4")
    except Exception:
        logger.exception("Failed to build Sheets service via ADC")
        return None


def _append_to_sheet(notices: list[NoticeRow], run_id: str, payload: ScraperJobPayload) -> int:
    sheet_id = (payload.gsheet_id or "").strip() or os.getenv("GSHEET_ID", "").strip()
    tab_name = (payload.gsheet_tab_name or "").strip() or os.getenv("GSHEET_TAB_NAME", "나라장터 공고 수집 목록").strip()
    if not sheet_id or not notices:
        return 0

    service = _build_sheets_service()
    if service is None:
        return 0

    logger.info("Appending %d notices to sheet_id=%s tab=%s", len(notices), sheet_id, tab_name)
    rows: list[list[str]] = []
    now_iso = datetime.now(timezone.utc).isoformat()
    for row in notices:
        rows.append(
            [
                now_iso,
                run_id,
                row.notice_id,
                row.title,
                row.agency,
                row.estimated_price,
                row.deadline_at.isoformat() if row.deadline_at else "",
                row.notice_url,
            ]
        )

    body = {"values": rows}
    try:
        service.spreadsheets().values().append(
            spreadsheetId=sheet_id,
            range=f"{tab_name}!A:H",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body=body,
        ).execute()
        return len(rows)
    except Exception:
        logger.exception("Failed to append notices to Google Sheet")
        return 0


def _report_run_result(
    *,
    run_id: str,
    status: str,
    payload: ScraperJobPayload,
    notice_count: int,
    deduped_count: int,
    email_sent_count: int,
    sheet_written_count: int,
    error_message: str | None,
    notices: list[NoticeRow],
) -> None:
    base_url = _backend_base_url()
    report_url = f"{base_url}/api/scraper/runs"
    body = {
        "run_id": run_id,
        "source": "cloud_run",
        "status": status,
        "keyword_count": len(payload.keywords),
        "notice_count": notice_count,
        "deduped_count": deduped_count,
        "email_sent_count": email_sent_count,
        "sheet_written_count": sheet_written_count,
        "error_message": error_message,
        "executed_at": datetime.now(timezone.utc).isoformat(),
        "notices": [notice.model_dump(mode="json") for notice in notices],
    }
    requests.post(report_url, headers=_backend_headers(), json=body, timeout=20).raise_for_status()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/run")
def run_scraper(job: ScraperJobPayload) -> dict[str, Any]:
    run_id = str(uuid4())

    if not job.enabled:
        _report_run_result(
            run_id=run_id,
            status="partial",
            payload=job,
            notice_count=0,
            deduped_count=0,
            email_sent_count=0,
            sheet_written_count=0,
            error_message="Scraper is disabled",
            notices=[],
        )
        return {"run_id": run_id, "status": "skipped", "message": "scraper disabled"}

    try:
        job.keywords = _normalize_string_list(job.keywords)
        job.receiver_emails = _normalize_string_list(job.receiver_emails)
        logger.info("Run %s: keywords=%s gsheet_id=%s", run_id, job.keywords, job.gsheet_id)

        try:
            notices = _fetch_g2b_notices(job.keywords)
        except Exception as error:
            raise RuntimeError(f"fetch failed: {error}") from error

        notice_count = len(notices)
        logger.info("Run %s: fetched %d notices", run_id, notice_count)
        try:
            deduped_notices = _dedup_with_backend(run_id, job, notices)
        except Exception as error:
            raise RuntimeError(f"dedup failed: {error}") from error

        deduped_count = max(0, notice_count - len(deduped_notices))
        try:
            mail_triggered = _trigger_apps_script_mail_webhook(
                run_id=run_id,
                payload=job,
                notices=deduped_notices,
            )
        except Exception:
            logger.exception("Apps Script webhook trigger failed")
            mail_triggered = False

        email_sent_count = 1 if mail_triggered else 0
        try:
            sheet_written_count = _append_to_sheet(deduped_notices, run_id, job)
        except Exception:
            logger.exception("Sheet write failed")
            sheet_written_count = 0

        status = "success"
        error_message = None
        if deduped_notices and (email_sent_count == 0 or sheet_written_count == 0):
            status = "partial"
            error_message = "At least one downstream sink was not written."

        _report_run_result(
            run_id=run_id,
            status=status,
            payload=job,
            notice_count=notice_count,
            deduped_count=deduped_count,
            email_sent_count=email_sent_count,
            sheet_written_count=sheet_written_count,
            error_message=error_message,
            notices=deduped_notices,
        )

        return {
            "run_id": run_id,
            "status": status,
            "notice_count": notice_count,
            "deduped_count": deduped_count,
            "email_sent_count": email_sent_count,
            "sheet_written_count": sheet_written_count,
        }
    except Exception as exc:
        logger.exception("Worker run failed")
        try:
            _report_run_result(
                run_id=run_id,
                status="failed",
                payload=job,
                notice_count=0,
                deduped_count=0,
                email_sent_count=0,
                sheet_written_count=0,
                error_message=str(exc),
                notices=[],
            )
        except Exception:
            pass
        raise HTTPException(status_code=500, detail=f"Scraper execution failed: {exc}") from exc
