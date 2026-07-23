"""Google Sheets writer for the isolated G2B bid-notice feature."""

import os
import re
from datetime import datetime
from pathlib import Path
from threading import Lock
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from app.features.g2b_bid_notice.schemas import BidNoticePreviewItem


SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
KST = ZoneInfo("Asia/Seoul")
SHEET_SAVE_LOCK = Lock()

SHEET_HEADERS = [
    "공고명",
    "공고번호",
    "업무구분",
    "게시일시 / 입찰마감일시",
    "수요기관",
    "세부절차",
    "세부절차상태",
    "사업금액",
    "업종제한(기관코드)",
    "공동도급",
    "지역제한",
    "원문",
    "첨부파일",
    "Sheets 저장일시",
]

LEGACY_SHEET_HEADERS = [
    "공고명",
    "공고번호",
    "차수",
    "게시일시",
    "입찰마감일시",
    "수요기관",
    "진행상태",
    "사업금액",
    "업종제한(기관코드)",
    "공동도급 여부",
    "지역제한 여부",
    "공고 원문 URL",
    "첨부파일",
    "Sheets 저장일시",
]


class SheetsIntegrationError(RuntimeError):
    """A safe, user-facing Google Sheets integration error."""


def _project_root() -> Path:
    # app/features/g2b_bid_notice/sheets.py -> icore-backend
    return Path(__file__).resolve().parents[3]


def _credential_file() -> Path:
    raw_path = os.getenv("GSHEET_SERVICE_ACCOUNT_FILE", "").strip()
    if not raw_path:
        raise SheetsIntegrationError("GSHEET_SERVICE_ACCOUNT_FILE 설정이 없습니다.")
    path = Path(raw_path)
    if not path.is_absolute():
        path = _project_root() / path
    if not path.is_file():
        raise SheetsIntegrationError("Google Sheets 서비스 계정 JSON 파일을 찾을 수 없습니다.")
    return path


def _settings() -> tuple[str, str]:
    spreadsheet_id = os.getenv("GSHEET_ID", "").strip()
    tab_name = os.getenv("GSHEET_TAB_NAME", "").strip()
    if not spreadsheet_id:
        raise SheetsIntegrationError("GSHEET_ID 설정이 없습니다.")
    if not tab_name:
        raise SheetsIntegrationError("GSHEET_TAB_NAME 설정이 없습니다.")
    return spreadsheet_id, tab_name


def _service():
    try:
        from google.oauth2.service_account import Credentials
        from googleapiclient.discovery import build
    except ImportError as error:
        raise SheetsIntegrationError("Google Sheets 연동 라이브러리가 설치되어 있지 않습니다.") from error

    credentials = Credentials.from_service_account_file(
        _credential_file(),
        scopes=[SHEETS_SCOPE],
    )
    return build("sheets", "v4", credentials=credentials, cache_discovery=False)


def _format_datetime(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.astimezone(KST).strftime("%Y-%m-%d %H:%M:%S")


def _format_amount(value: int | None) -> int | str:
    # Keep confirmed amounts numeric in Sheets; unknown must remain blank.
    return value if value is not None else ""


def _region_restriction_label(item: BidNoticePreviewItem) -> str:
    value = item.common_storage_record.region_restriction
    if value is True:
        return "제한 있음 (목록 원본값)"
    if value is False:
        return "제한 없음 (목록 원본값)"
    return "확인 필요 (상세·첨부파일 미확인)"


def _deduplicate_items(items: list[BidNoticePreviewItem]) -> list[BidNoticePreviewItem]:
    """Keep only one row per bid notice number for a single Sheet save."""

    unique_items: list[BidNoticePreviewItem] = []
    seen: set[str] = set()
    for item in items:
        bid_notice_no = (item.bid_notice_no or "").strip().casefold()
        dedup_key = f"notice:{bid_notice_no}" if bid_notice_no else f"record:{item.record_id}"
        if dedup_key not in seen:
            seen.add(dedup_key)
            unique_items.append(item)
    return unique_items


def _legacy_cell(row: list[object], index: int) -> object:
    return row[index] if index < len(row) else ""


def _migrate_legacy_sheet_row(row: list[object]) -> list[object]:
    """Keep existing values aligned when the Sheet moves to preview columns."""

    published_at = str(_legacy_cell(row, 3) or "")
    closing_at = str(_legacy_cell(row, 4) or "")
    dates = "\n".join(
        value
        for value in [
            f"게시 {published_at}" if published_at else "",
            f"입찰마감 {closing_at}" if closing_at else "",
        ]
        if value
    )
    return [
        _legacy_cell(row, 0),
        _legacy_cell(row, 1),
        "",
        dates,
        _legacy_cell(row, 5),
        "",
        "",
        _legacy_cell(row, 7),
        _legacy_cell(row, 8),
        _legacy_cell(row, 9),
        _legacy_cell(row, 10),
        _legacy_cell(row, 11),
        _legacy_cell(row, 12),
        _legacy_cell(row, 13),
    ]


def _ensure_headers(service, spreadsheet_id: str, tab_name: str) -> None:
    try:
        existing_values = (
            service.spreadsheets()
            .values()
            .get(spreadsheetId=spreadsheet_id, range=f"{tab_name}!A1:N1")
            .execute()
            .get("values", [])
        )
    except Exception as error:
        raise _translate_google_error(error) from error

    current_headers = existing_values[0] if existing_values else []
    if not current_headers:
        try:
            (
                service.spreadsheets()
                .values()
                .update(
                    spreadsheetId=spreadsheet_id,
                    range=f"{tab_name}!A1:N1",
                    valueInputOption="RAW",
                    body={"values": [SHEET_HEADERS]},
                )
                .execute()
            )
        except Exception as error:
            raise _translate_google_error(error) from error
        return

    if current_headers == SHEET_HEADERS:
        return

    if current_headers == LEGACY_SHEET_HEADERS:
        try:
            legacy_rows = (
                service.spreadsheets()
                .values()
                .get(spreadsheetId=spreadsheet_id, range=f"{tab_name}!A2:N")
                .execute()
                .get("values", [])
            )
            migrated_values = [SHEET_HEADERS] + [
                _migrate_legacy_sheet_row(row)
                for row in legacy_rows
                if isinstance(row, list)
            ]
            (
                service.spreadsheets()
                .values()
                .update(
                    spreadsheetId=spreadsheet_id,
                    range=f"{tab_name}!A1:N{len(migrated_values)}",
                    valueInputOption="RAW",
                    body={"values": migrated_values},
                )
                .execute()
            )
        except Exception as error:
            raise _translate_google_error(error) from error
        return

    if current_headers != SHEET_HEADERS:
        raise SheetsIntegrationError(
            "선택한 Sheet 첫 행의 헤더가 G2B 저장 형식과 다릅니다. "
            "빈 탭을 사용하거나 G2B용 헤더를 맞춘 뒤 다시 시도해주세요."
        )


def _translate_google_error(error: Exception) -> SheetsIntegrationError:
    status_code = getattr(getattr(error, "resp", None), "status", None)
    message = str(error).lower()
    if "invalid_grant" in message and "invalid jwt signature" in message:
        return SheetsIntegrationError(
            "Google 서비스 계정 JSON 키의 서명이 유효하지 않습니다. "
            "Google Cloud Console에서 현재 서비스 계정의 새 JSON 키를 발급한 뒤, "
            "GSHEET_SERVICE_ACCOUNT_FILE이 그 파일을 가리키도록 설정하세요."
        )
    if status_code == 403:
        return SheetsIntegrationError(
            "Sheet 접근 권한이 없습니다. 서비스 계정을 해당 Sheet의 편집자로 공유했는지 확인하세요."
        )
    if status_code == 404:
        return SheetsIntegrationError("Sheet 또는 탭 이름을 찾을 수 없습니다.")
    return SheetsIntegrationError("Google Sheets에 공고를 저장하지 못했습니다.")


def _existing_bid_notice_numbers(service, spreadsheet_id: str, tab_name: str) -> set[str]:
    """Read the Sheet's notice-number column after its header is confirmed."""

    try:
        values = (
            service.spreadsheets()
            .values()
            .get(spreadsheetId=spreadsheet_id, range=f"{tab_name}!B2:B")
            .execute()
            .get("values", [])
        )
    except Exception as error:
        raise _translate_google_error(error) from error

    return {
        str(row[0]).strip().casefold()
        for row in values
        if isinstance(row, list) and row and str(row[0]).strip()
    }


def append_selected_bid_notices(items: list[BidNoticePreviewItem]) -> tuple[int, int, str]:
    """Save new notice numbers directly below the header, newest first."""

    with SHEET_SAVE_LOCK:
        return _append_selected_bid_notices(items)


def _append_selected_bid_notices(items: list[BidNoticePreviewItem]) -> tuple[int, int, str]:
    unique_items = _deduplicate_items(items)
    if not unique_items:
        raise SheetsIntegrationError("저장할 공고가 없습니다.")

    spreadsheet_id, tab_name = _settings()
    service = _service()
    _ensure_headers(service, spreadsheet_id, tab_name)
    existing_notice_numbers = _existing_bid_notice_numbers(
        service,
        spreadsheet_id,
        tab_name,
    )
    items_to_save = [
        item
        for item in unique_items
        if not item.bid_notice_no
        or item.bid_notice_no.strip().casefold() not in existing_notice_numbers
    ]
    skipped_duplicate_count = len(unique_items) - len(items_to_save)
    if not items_to_save:
        return 0, skipped_duplicate_count, ""

    saved_at = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
    values = [_sheet_row(item, saved_at) for item in items_to_save]
    try:
        # Header is row 1. Insert the complete batch at row 2 first, so the
        # existing saved notices move down together and the latest save stays
        # at the top of the table.
        sheet_id = _sheet_id(service, spreadsheet_id, tab_name)
        response = (
            service.spreadsheets()
            .values()
            .update(
                spreadsheetId=spreadsheet_id,
                range=f"{tab_name}!A2",
                valueInputOption="RAW",
                body={"values": values},
            )
        )
        (
            service.spreadsheets()
            .batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={
                    "requests": [
                        {
                            "insertDimension": {
                                "range": {
                                    "sheetId": sheet_id,
                                    "dimension": "ROWS",
                                    "startIndex": 1,
                                    "endIndex": 1 + len(values),
                                },
                                "inheritFromBefore": False,
                            }
                        }
                    ]
                },
            )
            .execute()
        )
        response = response.execute()
    except Exception as error:
        raise _translate_google_error(error) from error

    updated_range = str(response.get("updatedRange") or "")
    # The direct URLs are already written as plain text in the row update.
    # Rich-text formatting makes each attachment filename directly clickable;
    # a formatting-only failure must not turn a successful Sheet save into an
    # apparent failure or invite the user to append a duplicate row.
    try:
        _apply_attachment_hyperlinks(
            service,
            spreadsheet_id,
            tab_name,
            updated_range,
            items_to_save,
        )
    except Exception:
        pass
    return len(items_to_save), skipped_duplicate_count, updated_range


def append_connection_test_row() -> str:
    """Append one clearly marked row to verify the service-account integration."""
    spreadsheet_id, tab_name = _settings()
    values = [[
        datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S"),
        "CONNECTION_TEST",
        "",
        "G2B 백엔드 ↔ Google Sheets 서비스 계정 연동 테스트",
        "",
        "성공 시 이 행이 자동으로 추가됩니다.",
    ]]
    try:
        response = (
            _service()
            .spreadsheets()
            .values()
            .append(
                spreadsheetId=spreadsheet_id,
                range=f"{tab_name}!A:F",
                valueInputOption="RAW",
                insertDataOption="INSERT_ROWS",
                body={"values": values},
            )
            .execute()
        )
    except SheetsIntegrationError:
        raise
    except Exception as error:
        status_code = getattr(getattr(error, "resp", None), "status", None)
        if status_code == 403:
            raise SheetsIntegrationError(
                "Sheet 접근 권한이 없습니다. 서비스 계정을 해당 Sheet의 편집자로 공유했는지 확인하세요."
            ) from error
        if status_code == 404:
            raise SheetsIntegrationError("Sheet 또는 탭 이름을 찾을 수 없습니다.") from error
        raise SheetsIntegrationError("Google Sheets에 테스트 행을 추가하지 못했습니다.") from error

    return str((response.get("updates") or {}).get("updatedRange") or "")


def _enrichment_label(value) -> str:
    """Keep unknown enrichment values visibly unknown in a saved Sheet row."""

    return value.label if value.state != "NOT_CHECKED" else "확인 전"


def _utf16_length(value: str) -> int:
    """Google Sheets text-format indexes use UTF-16 code units."""

    return len(value.encode("utf-16-le")) // 2


def _attachment_link_text(item: BidNoticePreviewItem) -> tuple[str, list[tuple[int, int, str]]]:
    """Return visible attachment URLs and their filename hyperlink ranges.

    The files remain on this PC, but the Sheet needs the original public G2B
    download URL so it can be opened from another device.
    """

    lines: list[str] = []
    link_ranges: list[tuple[int, int, str]] = []
    seen_urls: set[str] = set()

    for source in item.attachment_sources:
        url = source.download_url.strip()
        if url in seen_urls or urlparse(url).scheme not in {"http", "https"}:
            continue
        seen_urls.add(url)
        file_name = source.file_name.strip() or f"첨부파일 {len(lines) + 1}"
        prefix = "\n".join(lines)
        start_index = _utf16_length(prefix) + (1 if lines else 0)
        end_index = start_index + _utf16_length(file_name)
        lines.append(f"{file_name} | {url}")
        link_ranges.append((start_index, end_index, url))

    if not lines:
        return "첨부파일 링크 없음 또는 확인 필요", []
    return "\n".join(lines), link_ranges


def _attachment_paths(item: BidNoticePreviewItem) -> str:
    """Keep original G2B download URLs, rather than PC-only local paths."""

    return _attachment_link_text(item)[0]


def _start_row_from_updated_range(updated_range: str) -> int | None:
    """Read the first 1-based row number from a Sheets values write response."""

    match = re.search(r"!A(\d+):[A-Z]+\d+$", updated_range)
    return int(match.group(1)) if match else None


def _sheet_id(service, spreadsheet_id: str, tab_name: str) -> int:
    metadata = (
        service.spreadsheets()
        .get(
            spreadsheetId=spreadsheet_id,
            fields="sheets.properties(sheetId,title)",
        )
        .execute()
    )
    for sheet in metadata.get("sheets", []):
        properties = sheet.get("properties") or {}
        if properties.get("title") == tab_name:
            return int(properties["sheetId"])
    raise SheetsIntegrationError("저장 대상 Google Sheets 탭을 찾을 수 없습니다.")


def _apply_attachment_hyperlinks(
    service,
    spreadsheet_id: str,
    tab_name: str,
    updated_range: str,
    items: list[BidNoticePreviewItem],
) -> None:
    """Make every saved attachment filename a clickable original G2B link."""

    start_row = _start_row_from_updated_range(updated_range)
    if start_row is None:
        return

    sheet_id = _sheet_id(service, spreadsheet_id, tab_name)
    requests: list[dict] = []
    for row_offset, item in enumerate(items):
        text, link_ranges = _attachment_link_text(item)
        if not link_ranges:
            continue
        text_format_runs: list[dict] = []
        for start_index, end_index, url in link_ranges:
            text_format_runs.append(
                {"startIndex": start_index, "format": {"link": {"uri": url}}}
            )
            text_format_runs.append({"startIndex": end_index, "format": {}})
        requests.append(
            {
                "updateCells": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": start_row - 1 + row_offset,
                        "endRowIndex": start_row + row_offset,
                        "startColumnIndex": 12,
                        "endColumnIndex": 13,
                    },
                    "rows": [
                        {
                            "values": [
                                {
                                    "userEnteredValue": {"stringValue": text},
                                    "textFormatRuns": text_format_runs,
                                }
                            ]
                        }
                    ],
                    "fields": "userEnteredValue,textFormatRuns",
                }
            }
        )
    if requests:
        service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": requests},
        ).execute()


def _preview_dates(item: BidNoticePreviewItem) -> str:
    dates = [
        label
        for label in [
            f"게시 {_format_datetime(item.published_at)}" if item.published_at else "",
            f"입찰마감 {_format_datetime(item.bid_closing_at)}" if item.bid_closing_at else "",
        ]
        if label
    ]
    return "\n".join(dates)


def _sheet_row(item: BidNoticePreviewItem, saved_at: str) -> list[object]:
    return [
        item.business_name or "",
        item.bid_notice_no or "",
        item.work_type or "확인 필요",
        _preview_dates(item),
        item.demand_agency_name or "",
        item.detail_procedure or "확인 필요",
        item.detail_procedure_status or "확인 필요",
        _format_amount(item.business_amount),
        _enrichment_label(item.industry_restriction),
        _enrichment_label(item.joint_contracting),
        _enrichment_label(item.region_restriction_detail),
        item.source_url or "",
        _attachment_paths(item),
        saved_at,
    ]
