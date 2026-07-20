import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Literal
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.g2b.opening_results.models import BidOpeningEntryModel, BidOpeningRoundModel
from app.g2b.opening_results.notice_context_repository import (
    canonical_notice_key,
    load_bid_notice_contexts,
)
from app.g2b.opening_results.schemas import BidNoticeSheetContext


SHEET_HEADERS = [
    "공고번호",
    "사업명",
    "발주처",
    "사업금액",
    "제안마감",
    "지역제한여부",
    "2단계 입찰(여부)",
    "1위(이름)",
    "1위 총점(점수)",
    "2위(이름)",
    "2위 총점(점수)",
    "3위(이름)",
    "3위 총점(점수)",
    "4위(이름)",
    "4위 총점(점수)",
    "5위(이름)",
    "5위 총점(점수)",
]
LEGACY_SHEET_HEADERS = [
    *SHEET_HEADERS[:3],
    "기초금액",
    *SHEET_HEADERS[4:],
]


class SheetExportConfigurationError(RuntimeError):
    pass


@dataclass(frozen=True)
class SheetUpsertResult:
    inserted_count: int
    updated_count: int


@dataclass(frozen=True)
class SheetConnectionVerification:
    spreadsheet_title: str | None
    tab_exists: bool
    header_status: str


@dataclass(frozen=True)
class RankedOpeningEntry:
    rank: int
    entry: BidOpeningEntryModel
    source: Literal["OFFICIAL", "SCORE_CALCULATED"]


def organize_entry_rankings(
    entries: list[BidOpeningEntryModel],
    *,
    limit: int = 5,
) -> list[RankedOpeningEntry]:
    """공식 순위를 우선하고, 누락된 순위만 공개 종합점수 순으로 보충한다."""
    ranked: dict[int, RankedOpeningEntry] = {}
    for entry in sorted(
        entries,
        key=lambda item: (item.rank is None, item.rank or 0, item.id),
    ):
        if entry.rank is None or not 1 <= entry.rank <= limit or entry.rank in ranked:
            continue
        ranked[entry.rank] = RankedOpeningEntry(
            rank=entry.rank,
            entry=entry,
            source="OFFICIAL",
        )

    available_ranks = [rank for rank in range(1, limit + 1) if rank not in ranked]
    score_candidates: list[tuple[Decimal, BidOpeningEntryModel]] = []
    for entry in entries:
        if entry.rank is not None:
            continue
        score = entry.official_total_score
        if score is None:
            score = entry.total_score
        if score is not None:
            score_candidates.append((score, entry))
    score_candidates.sort(key=lambda item: (-item[0], item[1].id))

    for rank, (_, entry) in zip(available_ranks, score_candidates, strict=False):
        ranked[rank] = RankedOpeningEntry(
            rank=rank,
            entry=entry,
            source="SCORE_CALCULATED",
        )
    return [ranked[rank] for rank in sorted(ranked)]


def get_sheet_service_account_email() -> str | None:
    explicit_email = os.getenv("GSHEET_SERVICE_ACCOUNT_EMAIL", "").strip()
    if explicit_email:
        return explicit_email

    inline_json = os.getenv("GSHEET_SERVICE_ACCOUNT_JSON", "").strip()
    if inline_json:
        try:
            email = str(json.loads(inline_json).get("client_email") or "").strip()
        except (json.JSONDecodeError, AttributeError):
            email = ""
        if email:
            return email

    credential_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
    if credential_path:
        try:
            with open(credential_path, encoding="utf-8") as credential_file:
                email = str(json.load(credential_file).get("client_email") or "").strip()
        except (OSError, json.JSONDecodeError, AttributeError):
            email = ""
        if email:
            return email
    return None


def _sheet_number(value: Decimal | None) -> str | int | float:
    if value is None:
        return ""
    if value == value.to_integral_value():
        return int(value)
    return float(value)


def _sheet_datetime(value: datetime | None) -> str:
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=ZoneInfo("Asia/Seoul"))
    return value.astimezone(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d %H:%M")


def _sheet_score_breakdown(
    entry: BidOpeningEntryModel | None,
) -> str | int | float:
    if entry is None:
        return ""
    if entry.bid_price_score is None or entry.technical_score is None:
        return ""

    total_score = entry.bid_price_score + entry.technical_score
    price_text = format(entry.bid_price_score.normalize(), "f")
    technical_text = format(entry.technical_score.normalize(), "f")
    total_text = format(
        total_score.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
        ".2f",
    )
    return f"{price_text}+{technical_text}={total_text}"


def build_sheet_row(
    round_row: BidOpeningRoundModel,
    entries: list[BidOpeningEntryModel],
    context: BidNoticeSheetContext | None,
) -> list[str | int | float]:
    ranked = {item.rank: item.entry for item in organize_entry_rankings(entries)}
    business_amount: str | int | float = _sheet_number(
        context.base_amount if context else None
    )
    row: list[str | int | float] = [
        round_row.bid_notice_no,
        (context.business_name if context else None) or "",
        (context.demand_agency_name if context else None) or "",
        business_amount,
        _sheet_datetime(context.proposal_deadline if context else None),
        (context.region_restriction if context else None) or "",
        (
            "Y"
            if context and context.is_two_stage_bid is True
            else "N"
            if context and context.is_two_stage_bid is False
            else ""
        ),
    ]
    for rank in range(1, 6):
        entry = ranked.get(rank)
        row.extend(
            [
                (entry.company_name if entry else None) or "",
                _sheet_score_breakdown(entry),
            ]
        )
    return row


def _has_complete_notice_context(context: BidNoticeSheetContext | None) -> bool:
    if context is None:
        return False
    return bool(
        context.business_name
        and context.business_name.strip()
        and context.demand_agency_name
        and context.demand_agency_name.strip()
        and context.base_amount is not None
        and context.proposal_deadline is not None
        and context.region_restriction
        and context.region_restriction.strip()
        and context.is_two_stage_bid is not None
    )


def build_sheet_rows(
    db: Session,
    result_ids: list[int],
    *,
    selected_rounds: list[BidOpeningRoundModel] | None = None,
) -> tuple[list[list[str | int | float]], list[str], list[int]]:
    unique_result_ids = list(dict.fromkeys(result_ids))
    if selected_rounds is None:
        selected_rounds = (
            db.execute(
                select(BidOpeningRoundModel).where(
                    BidOpeningRoundModel.id.in_(unique_result_ids)
                )
            )
            .scalars()
            .all()
        )
    round_by_id = {round_row.id: round_row for round_row in selected_rounds}
    missing_result_ids = [
        result_id for result_id in unique_result_ids if result_id not in round_by_id
    ]
    ordered_rounds = [
        round_by_id[result_id]
        for result_id in unique_result_ids
        if result_id in round_by_id
    ]
    context_by_key = load_bid_notice_contexts(
        db,
        [
            (round_row.bid_notice_no, round_row.bid_notice_ord)
            for round_row in ordered_rounds
        ],
    )

    entries_by_round: dict[int, list[BidOpeningEntryModel]] = {
        round_row.id: [] for round_row in ordered_rounds
    }
    if entries_by_round:
        entries = (
            db.execute(
                select(BidOpeningEntryModel).where(
                    BidOpeningEntryModel.round_id.in_(entries_by_round.keys())
                )
            )
            .scalars()
            .all()
        )
        for entry in entries:
            entries_by_round[entry.round_id].append(entry)

    missing_context_keys: list[str] = []
    rows: list[list[str | int | float]] = []
    for round_row in ordered_rounds:
        key = canonical_notice_key(round_row.bid_notice_no, round_row.bid_notice_ord)
        context = context_by_key.get(key)
        if not _has_complete_notice_context(context):
            missing_context_keys.append(
                f"{round_row.bid_notice_no}|{round_row.bid_notice_ord}"
            )
        rows.append(build_sheet_row(round_row, entries_by_round[round_row.id], context))
    return rows, missing_context_keys, missing_result_ids


def find_duplicate_notice_numbers(
    rows: list[list[str | int | float]],
) -> list[str]:
    seen: set[str] = set()
    duplicates: list[str] = []
    for row in rows:
        notice_no = str(row[0]).strip()
        if notice_no in seen and notice_no not in duplicates:
            duplicates.append(notice_no)
        seen.add(notice_no)
    return duplicates


def build_sheet_preview_token(
    destination_id: int,
    spreadsheet_id: str,
    tab_name: str,
    result_ids: list[int],
    rows: list[list[str | int | float]],
) -> str:
    payload = json.dumps(
        {
            "destination_id": destination_id,
            "spreadsheet_id": spreadsheet_id,
            "tab_name": tab_name,
            "result_ids": result_ids,
            "headers": SHEET_HEADERS,
            "rows": rows,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class GoogleSheetWriter:
    def __init__(self, spreadsheet_id: str, tab_name: str, service: Any | None = None) -> None:
        if not spreadsheet_id.strip():
            raise SheetExportConfigurationError(
                "spreadsheet_id 또는 GSHEET_OPENING_RESULT_ID가 필요합니다."
            )
        self.spreadsheet_id = spreadsheet_id.strip()
        self.tab_name = tab_name.strip() or "개찰결과"
        self.service = service or self._build_service()

    @classmethod
    def from_env(
        cls,
        *,
        spreadsheet_id: str | None = None,
        tab_name: str = "개찰결과",
    ) -> "GoogleSheetWriter":
        final_id = (
            spreadsheet_id
            or os.getenv("GSHEET_OPENING_RESULT_ID")
            or os.getenv("GSHEET_ID")
            or ""
        )
        return cls(final_id, tab_name)

    @staticmethod
    def _build_service():
        try:
            from google.oauth2 import service_account
            from googleapiclient.discovery import build
        except ImportError as error:
            raise SheetExportConfigurationError(
                "Google Sheets 클라이언트 의존성이 설치되지 않았습니다."
            ) from error

        inline_json = os.getenv("GSHEET_SERVICE_ACCOUNT_JSON", "").strip()
        if inline_json:
            credentials = service_account.Credentials.from_service_account_info(
                json.loads(inline_json),
                scopes=["https://www.googleapis.com/auth/spreadsheets"],
            )
            return build("sheets", "v4", credentials=credentials)
        return build("sheets", "v4")

    def verify_connection(self) -> SheetConnectionVerification:
        metadata = (
            self.service.spreadsheets()
            .get(
                spreadsheetId=self.spreadsheet_id,
                fields="properties.title,sheets.properties.title",
            )
            .execute()
        )
        spreadsheet_title = str(
            (metadata.get("properties") or {}).get("title") or ""
        ).strip() or None
        tab_titles = {
            str((sheet.get("properties") or {}).get("title") or "").strip()
            for sheet in metadata.get("sheets") or []
        }
        if self.tab_name not in tab_titles:
            return SheetConnectionVerification(
                spreadsheet_title=spreadsheet_title,
                tab_exists=False,
                header_status="NOT_CHECKED",
            )

        escaped_tab = self.tab_name.replace("'", "''")
        header_response = (
            self.service.spreadsheets()
            .values()
            .get(
                spreadsheetId=self.spreadsheet_id,
                range=f"'{escaped_tab}'!A1:Q1",
            )
            .execute()
        )
        header_values = header_response.get("values") or []
        if not header_values or not any(str(value).strip() for value in header_values[0]):
            header_status = "EMPTY"
        elif tuple(header_values[0][: len(SHEET_HEADERS)]) in {
            tuple(SHEET_HEADERS),
            tuple(LEGACY_SHEET_HEADERS),
        }:
            header_status = "MATCH"
        else:
            header_status = "MISMATCH"
        return SheetConnectionVerification(
            spreadsheet_title=spreadsheet_title,
            tab_exists=True,
            header_status=header_status,
        )

    def upsert(self, rows: list[list[str | int | float]]) -> SheetUpsertResult:
        escaped_tab = self.tab_name.replace("'", "''")
        spreadsheets_api = self.service.spreadsheets()
        values_api = spreadsheets_api.values()
        header_response = values_api.get(
            spreadsheetId=self.spreadsheet_id,
            range=f"'{escaped_tab}'!A1:Q1",
        ).execute()
        header_values = header_response.get("values") or []
        if not header_values:
            values_api.update(
                spreadsheetId=self.spreadsheet_id,
                range=f"'{escaped_tab}'!A1",
                valueInputOption="RAW",
                body={"values": [SHEET_HEADERS]},
            ).execute()
        elif header_values[0][: len(SHEET_HEADERS)] == LEGACY_SHEET_HEADERS:
            values_api.update(
                spreadsheetId=self.spreadsheet_id,
                range=f"'{escaped_tab}'!A1",
                valueInputOption="RAW",
                body={"values": [SHEET_HEADERS]},
            ).execute()
        elif header_values[0][: len(SHEET_HEADERS)] != SHEET_HEADERS:
            raise SheetExportConfigurationError(
                "개찰결과 Sheet의 A:Q 헤더가 고정 17개 열과 일치하지 않습니다."
            )

        existing_response = values_api.get(
            spreadsheetId=self.spreadsheet_id,
            range=f"'{escaped_tab}'!A2:Q",
        ).execute()
        existing_rows = existing_response.get("values") or []
        row_number_by_notice: dict[str, int] = {}
        for row_number, existing_row in enumerate(existing_rows, start=2):
            if not existing_row or not str(existing_row[0]).strip():
                continue
            notice_no = str(existing_row[0]).strip()
            if notice_no in row_number_by_notice:
                raise SheetExportConfigurationError(
                    f"기존 Sheet에 공고번호 {notice_no}가 중복되어 있습니다."
                )
            row_number_by_notice[notice_no] = row_number

        write_data = []
        insert_rows = []
        next_row_number = len(existing_rows) + 2
        for row in rows:
            notice_no = str(row[0]).strip()
            existing_row_number = row_number_by_notice.get(notice_no)
            if existing_row_number is None:
                insert_rows.append(row)
                row_number_by_notice[notice_no] = -1
                write_data.append(
                    {
                        "range": f"'{escaped_tab}'!A{next_row_number}:Q{next_row_number}",
                        "values": [row],
                    }
                )
                next_row_number += 1
                continue
            write_data.append(
                {
                    "range": (
                        f"'{escaped_tab}'!A{existing_row_number}:Q{existing_row_number}"
                    ),
                    "values": [row],
                }
            )

        if write_data:
            metadata = spreadsheets_api.get(
                spreadsheetId=self.spreadsheet_id,
                fields="sheets.properties(sheetId,title)",
            ).execute()
            sheet_id = next(
                (
                    sheet["properties"]["sheetId"]
                    for sheet in metadata.get("sheets") or []
                    if sheet.get("properties", {}).get("title") == self.tab_name
                ),
                None,
            )
            if sheet_id is None:
                raise SheetExportConfigurationError(
                    f"Google Sheet에서 {self.tab_name} 탭을 찾을 수 없습니다."
                )
            values_api.batchUpdate(
                spreadsheetId=self.spreadsheet_id,
                body={
                    "valueInputOption": "RAW",
                    "data": write_data,
                },
            ).execute()
            spreadsheets_api.batchUpdate(
                spreadsheetId=self.spreadsheet_id,
                body={
                    "requests": [
                        {
                            "repeatCell": {
                                "range": {
                                    "sheetId": sheet_id,
                                    "startRowIndex": 1,
                                    "startColumnIndex": 3,
                                    "endColumnIndex": 4,
                                },
                                "cell": {
                                    "userEnteredFormat": {
                                        "numberFormat": {
                                            "type": "NUMBER",
                                            "pattern": "#,##0",
                                        }
                                    }
                                },
                                "fields": "userEnteredFormat.numberFormat",
                            }
                        }
                    ]
                },
            ).execute()
        return SheetUpsertResult(
            inserted_count=len(insert_rows),
            updated_count=len(write_data) - len(insert_rows),
        )
