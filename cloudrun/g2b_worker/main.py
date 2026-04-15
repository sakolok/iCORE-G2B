import base64
import html
import json
import os
import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Any
from email.message import EmailMessage
from urllib.parse import parse_qsl, urlparse
from uuid import uuid4
from zoneinfo import ZoneInfo

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
    matched_keyword: str = ""
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
    gsheet_ids: list[str] = Field(default_factory=list)
    gsheet_tab_name: str = "나라장터 공고 수집 목록"
    receiver_emails: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)


def _normalize_string_list(raw: Any) -> list[str]:
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    if isinstance(raw, str):
        return [item.strip() for item in raw.split(",") if item.strip()]
    return []


def _resolve_sheet_ids(payload: ScraperJobPayload) -> list[str]:
    """
    Backward/forward compatible sheet id resolver.
    - Legacy worker payload: gsheet_id
    - Current backend payload: gsheet_ids
    """
    ids: list[str] = []
    if payload.gsheet_id and payload.gsheet_id.strip():
        ids.append(payload.gsheet_id.strip())
    ids.extend(item.strip() for item in payload.gsheet_ids if str(item).strip())
    # keep order, remove duplicates
    seen: set[str] = set()
    unique_ids: list[str] = []
    for sheet_id in ids:
        if sheet_id not in seen:
            unique_ids.append(sheet_id)
            seen.add(sheet_id)
    return unique_ids


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
                parsed.matched_keyword = keyword
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
                "sheet_ids": _resolve_sheet_ids(payload) or [os.getenv("GSHEET_ID", "").strip()],
                "sheet_tab_name": payload.gsheet_tab_name,
                "notice_count": len(notices),
            },
        )
        return 200 <= response.status_code < 300
    except Exception:
        return False


GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"


def _gmail_service_account_info() -> dict[str, Any] | None:
    """
    Domain-wide delegation needs service-account JSON (same pattern as Sheets).
    - GMAIL_SERVICE_ACCOUNT_JSON: preferred when set
    - Else GSHEET_SERVICE_ACCOUNT_JSON if the same SA is used
    """
    raw = os.getenv("GMAIL_SERVICE_ACCOUNT_JSON", "").strip()
    if not raw:
        raw = os.getenv("GSHEET_SERVICE_ACCOUNT_JSON", "").strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        logger.exception("Invalid JSON in GMAIL_SERVICE_ACCOUNT_JSON / GSHEET_SERVICE_ACCOUNT_JSON")
        return None


def _gmail_delegated_user() -> str:
    """Workspace user to impersonate (send-as mailbox), e.g. notice-bot@domain."""
    return os.getenv("GMAIL_DELEGATED_USER", "").strip()


def _build_gmail_service():
    if build is None or service_account is None:
        logger.warning("google-api-python-client / google-auth not available for Gmail")
        return None
    info = _gmail_service_account_info()
    delegated = _gmail_delegated_user()
    if not info or not delegated:
        return None
    try:
        credentials = service_account.Credentials.from_service_account_info(
            info,
            scopes=[GMAIL_SEND_SCOPE],
            subject=delegated,
        )
        return build("gmail", "v1", credentials=credentials)
    except Exception:
        logger.exception("Failed to build Gmail API service")
        return None


def _format_notice_amount_for_email(raw: str) -> str:
    """Format currency-like strings the same way as sheet append."""
    text = (raw or "").strip()
    if not text:
        return ""
    if "~" in text or "-" in text:
        return text
    digits_only = re.sub(r"[^\d]", "", text)
    if not digits_only:
        return text
    number_tokens = re.findall(r"\d+", text.replace(",", ""))
    if len(number_tokens) != 1:
        return text
    try:
        value = int(digits_only)
    except Exception:
        return text
    return f"{value:,}"


def _deadline_kst_display(dt: datetime | None) -> str:
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d %H:%M")


def _notices_to_html_table(notices: list[NoticeRow]) -> str:
    rows_html: list[str] = []
    for n in notices:
        link = (n.notice_url or "").strip()
        link_cell = (
            f'<a href="{html.escape(link, quote=True)}">보기</a>' if link else ""
        )
        rows_html.append(
            "<tr>"
            f"<td>{html.escape((n.matched_keyword or '').strip())}</td>"
            f"<td>{html.escape((n.title or '').strip())}</td>"
            f"<td>{html.escape((n.agency or '').strip())}</td>"
            f"<td style=\"text-align:right\">{html.escape(_format_notice_amount_for_email(n.estimated_price or ''))}</td>"
            f"<td>{html.escape(_deadline_kst_display(n.deadline_at))}</td>"
            f"<td>{link_cell}</td>"
            "</tr>"
        )
    return (
        "<table border=\"1\" cellpadding=\"6\" cellspacing=\"0\" "
        "style=\"border-collapse:collapse;font-family:sans-serif;font-size:14px\">"
        "<thead><tr>"
        "<th>키워드</th><th>공고명</th><th>기관</th><th>추정가격</th><th>마감</th><th>링크</th>"
        "</tr></thead><tbody>"
        + "".join(rows_html)
        + "</tbody></table>"
    )


def _send_gmail_notice_digest(
    *,
    run_id: str,
    payload: ScraperJobPayload,
    notices: list[NoticeRow],
) -> bool:
    """
    Send via Gmail API using domain-wide delegation.
    Recipients are Bcc so addresses stay private.
    """
    if not notices or not payload.receiver_emails:
        return False
    service = _build_gmail_service()
    if service is None:
        return False

    send_as = _gmail_delegated_user()
    keywords_preview = ", ".join(payload.keywords[:8])
    if len(payload.keywords) > 8:
        keywords_preview += " …"
    now_kst = datetime.now(timezone.utc).astimezone(ZoneInfo("Asia/Seoul"))
    subject = (
        f"[나라장터] 신규 공고 {len(notices)}건 "
        f"({now_kst:%Y-%m-%d %H:%M} KST, run {run_id[:8]})"
    )
    if keywords_preview:
        subject += f" | 키워드: {keywords_preview}"

    inner_table = _notices_to_html_table(notices)
    html_body = f"""<!DOCTYPE html>
<html>
  <body style="font-family:sans-serif">
    <p>수집 시각(KST): {html.escape(now_kst.strftime("%Y-%m-%d %H:%M:%S"))}</p>
    <p>run_id: <code>{html.escape(run_id)}</code></p>
    <p>신규 공고 <strong>{len(notices)}</strong>건입니다.</p>
    {inner_table}
  </body>
</html>"""

    plain_lines = [
        f"수집 시각(KST): {now_kst:%Y-%m-%d %H:%M:%S}",
        f"run_id: {run_id}",
        f"신규 공고 {len(notices)}건",
        "",
    ]
    for n in notices:
        plain_lines.append(
            " | ".join(
                [
                    (n.matched_keyword or "").strip(),
                    (n.title or "").strip(),
                    (n.agency or "").strip(),
                    _format_notice_amount_for_email(n.estimated_price or ""),
                    _deadline_kst_display(n.deadline_at),
                    (n.notice_url or "").strip(),
                ]
            )
        )
    plain_body = "\n".join(plain_lines)

    msg = EmailMessage()
    msg["From"] = send_as
    msg["To"] = send_as
    msg["Bcc"] = ", ".join(payload.receiver_emails)
    msg["Subject"] = subject
    msg.set_content(plain_body)
    msg.add_alternative(html_body, subtype="html")

    try:
        encoded = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        service.users().messages().send(userId="me", body={"raw": encoded}).execute()
        logger.info("Gmail send ok run_id=%s recipients=%d", run_id, len(payload.receiver_emails))
        return True
    except Exception:
        logger.exception("Gmail API send failed run_id=%s", run_id)
        return False


def _notify_recipients(
    *,
    run_id: str,
    payload: ScraperJobPayload,
    notices: list[NoticeRow],
) -> bool:
    """Prefer Gmail when SA JSON + delegated user env are set; else Apps Script webhook."""
    if _gmail_service_account_info() and _gmail_delegated_user():
        return _send_gmail_notice_digest(run_id=run_id, payload=payload, notices=notices)
    return _trigger_apps_script_mail_webhook(run_id=run_id, payload=payload, notices=notices)


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
    sheet_ids = _resolve_sheet_ids(payload)
    sheet_id = (sheet_ids[0] if sheet_ids else "") or os.getenv("GSHEET_ID", "").strip()
    tab_name = (payload.gsheet_tab_name or "").strip() or os.getenv("GSHEET_TAB_NAME", "나라장터 공고 수집 목록").strip()
    if not sheet_id:
        return 0

    service = _build_sheets_service()
    if service is None:
        return 0

    logger.info("Appending %d notices to sheet_id=%s tab=%s", len(notices), sheet_id, tab_name)

    # n번째 수집 계산: A열에서 숫자인 값만 모아 max+1 (공고 행 A열은 키워드 문자열)
    run_no = 1
    try:
        col_a = (
            service.spreadsheets()
            .values()
            .get(spreadsheetId=sheet_id, range=f"{tab_name}!A:A")
            .execute()
            .get("values", [])
        )
        max_seen = 0
        for row in col_a:
            if not row:
                continue
            raw = str(row[0]).strip()
            if raw.isdigit():
                max_seen = max(max_seen, int(raw))
        run_no = max_seen + 1
    except Exception:
        logger.exception("Failed to compute run number from sheet. Defaulting to 1.")
        run_no = 1

    now_kst = datetime.now(timezone.utc).astimezone(ZoneInfo("Asia/Seoul"))
    collected_at = now_kst.strftime("%Y-%m-%d %H:%M:%S")

    # 형식:
    # (빈줄)
    # [n번째 수집, 수집시각, ...]
    # [키워드, 공고명, 기관, 추정가격, 마감일시, 링크]
    values_to_write: list[list[str]] = []
    values_to_write.append(["", "", "", "", "", ""])
    values_to_write.append([str(run_no), collected_at, "", "", "", ""])

    def _format_krw_commas(raw: str) -> str:
        """
        Normalize an amount-like string into comma-separated KRW number.
        - If input looks like a single integer amount (possibly with commas / suffix), format with commas.
        - If it contains multiple numbers or non-trivial text (ranges), keep as-is.
        """
        text = (raw or "").strip()
        if not text:
            return ""
        # If it looks like a range or composite (e.g. "1,000~2,000"), keep original.
        if "~" in text or "-" in text:
            return text

        digits_only = re.sub(r"[^\d]", "", text)
        if not digits_only:
            return text

        # If original has multiple separate numbers (e.g. "10억 2천"), be conservative.
        number_tokens = re.findall(r"\d+", text.replace(",", ""))
        if len(number_tokens) != 1:
            return text

        try:
            value = int(digits_only)
        except Exception:
            return text
        return f"{value:,}"

    def _normalize_link_cell(raw: str) -> str:
        text = (raw or "").strip()
        if not text:
            return ""
        # 이미 시트 수식 형태로 내려오면 그대로 사용
        if "HYPERLINK(" in text.upper():
            return text if text.startswith("=") else f"={text}"
        return f'=HYPERLINK("{text}","보기")'

    def _deadline_display(dt: datetime | None) -> str:
        if dt is None:
            return ""
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d %H:%M")

    for n in notices:
        link_cell = _normalize_link_cell(n.notice_url or "")
        values_to_write.append(
            [
                (n.matched_keyword or "").strip(),
                (n.title or "").strip(),
                (n.agency or "").strip(),
                _format_krw_commas(n.estimated_price or ""),
                _deadline_display(n.deadline_at),
                link_cell,
            ]
        )

    body = {"values": values_to_write}
    def _a1_to_col_index(col: str) -> int:
        col = (col or "").strip().upper()
        idx = 0
        for ch in col:
            if "A" <= ch <= "Z":
                idx = idx * 26 + (ord(ch) - ord("A") + 1)
        return max(0, idx - 1)

    def _parse_a1_range(a1: str) -> tuple[str, int, int, int, int] | None:
        # e.g. "Tab!A10:F20" -> (tab, 9, 0, 20, 6) [0-based start row/col, end row/col exclusive]
        if not a1 or "!" not in a1 or ":" not in a1:
            return None
        tab, rng = a1.split("!", 1)
        start_ref, end_ref = rng.split(":", 1)

        def _split_ref(ref: str) -> tuple[str, int] | None:
            m = re.match(r"^([A-Za-z]+)(\d+)$", (ref or "").strip())
            if not m:
                return None
            return m.group(1), int(m.group(2))

        start = _split_ref(start_ref)
        end = _split_ref(end_ref)
        if start is None or end is None:
            return None
        start_col = _a1_to_col_index(start[0])
        start_row = max(0, start[1] - 1)
        end_col = _a1_to_col_index(end[0]) + 1
        end_row = max(start_row + 1, end[1])
        return tab, start_row, start_col, end_row, end_col

    def _get_sheet_numeric_id() -> int | None:
        try:
            meta = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
            for s in meta.get("sheets", []) or []:
                props = (s or {}).get("properties", {}) or {}
                if str(props.get("title") or "").strip() == tab_name:
                    return props.get("sheetId")
        except Exception:
            logger.exception("Failed to resolve sheet numeric id for formatting")
        return None

    def _apply_append_formatting(updated_range: str, notice_row_count: int) -> None:
        parsed = _parse_a1_range(updated_range)
        if parsed is None:
            return
        _, start_row, _, end_row, _ = parsed
        sheet_numeric_id = _get_sheet_numeric_id()
        if sheet_numeric_id is None:
            return

        # 우리가 추가한 행 수 = 빈줄 1 + 런헤더 1 + 공고 n
        appended_rows = 2 + max(0, notice_row_count)
        # updatedRange가 더 크게 잡힐 수 있어(기존 데이터와 합쳐진 범위 등) 우리가 쓴 만큼만 포맷 적용
        target_start = start_row
        target_end = min(end_row, start_row + appended_rows)

        white = {"red": 1.0, "green": 1.0, "blue": 1.0}
        gray = {"red": 0.9, "green": 0.9, "blue": 0.9}

        run_row = target_start + 1

        requests_payload = [
            # 1) 추가된 범위는 기본 흰색 배경으로 리셋 (서식 복제 방지)
            {
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_numeric_id,
                        "startRowIndex": target_start,
                        "endRowIndex": target_end,
                        "startColumnIndex": 0,
                        "endColumnIndex": 6,
                    },
                    "cell": {"userEnteredFormat": {"backgroundColor": white}},
                    "fields": "userEnteredFormat.backgroundColor",
                }
            },
            # 2) 런 번호(A) + 수집시각(B) 셀만 회색
            {
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_numeric_id,
                        "startRowIndex": run_row,
                        "endRowIndex": run_row + 1,
                        "startColumnIndex": 0,
                        "endColumnIndex": 2,
                    },
                    "cell": {"userEnteredFormat": {"backgroundColor": gray}},
                    "fields": "userEnteredFormat.backgroundColor",
                }
            },
        ]

        try:
            service.spreadsheets().batchUpdate(
                spreadsheetId=sheet_id,
                body={"requests": requests_payload},
            ).execute()
        except Exception:
            logger.exception("Failed to apply sheet formatting updates")

    try:
        append_result = service.spreadsheets().values().append(
            spreadsheetId=sheet_id,
            range=f"{tab_name}!A:F",
            # HYPERLINK 같은 수식을 정상 동작시키려면 USER_ENTERED 가 필요
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body=body,
        ).execute()
        updated_range = ((append_result or {}).get("updates") or {}).get("updatedRange") or ""
        if updated_range:
            _apply_append_formatting(updated_range, len(notices))
        # 공고 행 수만 반환
        return len(notices)
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
        job.gsheet_ids = _normalize_string_list(job.gsheet_ids)
        logger.info(
            "Run %s: keywords=%s gsheet_id=%s gsheet_ids=%s",
            run_id,
            job.keywords,
            job.gsheet_id,
            job.gsheet_ids,
        )

        try:
            notices = _fetch_g2b_notices(job.keywords)
        except Exception as error:
            raise RuntimeError(f"fetch failed: {error}") from error

        notice_count = len(notices)
        logger.info("Run %s: fetched %d notices", run_id, notice_count)
        try:
            keyword_map: dict[tuple[str, str], str] = {}
            for notice in notices:
                key = ((notice.notice_id or "").strip(), (notice.title or "").strip())
                if key != ("", "") and key not in keyword_map:
                    keyword_map[key] = (notice.matched_keyword or "").strip()
            deduped_notices = _dedup_with_backend(run_id, job, notices)
            # 백엔드 dedup 스키마에는 matched_keyword가 없어서 떨어질 수 있으므로 복원
            for notice in deduped_notices:
                key = ((notice.notice_id or "").strip(), (notice.title or "").strip())
                restored = keyword_map.get(key, "")
                if restored:
                    notice.matched_keyword = restored
        except Exception as error:
            raise RuntimeError(f"dedup failed: {error}") from error

        deduped_count = max(0, notice_count - len(deduped_notices))
        try:
            sheet_written_count = _append_to_sheet(deduped_notices, run_id, job)
        except Exception:
            logger.exception("Sheet write failed")
            sheet_written_count = 0

        try:
            mail_triggered = _notify_recipients(
                run_id=run_id,
                payload=job,
                notices=deduped_notices,
            )
        except Exception:
            logger.exception("Notification (Gmail / Apps Script) failed")
            mail_triggered = False

        email_sent_count = 1 if mail_triggered else 0

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
