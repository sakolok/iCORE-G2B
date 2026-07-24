import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from app.data.models import ScraperNoticeModel
from app.g2b.bid_notice import KST
from app.g2b.bid_notices.models import BidNoticeSheetExportModel
from app.g2b.opening_results.matching import SheetExportConflictError
from app.g2b.opening_results.models import SheetDestinationModel
from app.g2b.opening_results.sheet_export import (
    GoogleSheetWriter,
    SheetExportConfigurationError,
    SheetUpsertResult,
)


BID_NOTICE_SHEET_HEADERS = [
    "공고번호",
    "공고명",
    "게시일시(입찰마감일시)",
    "수요기관",
    "사업금액",
    "업종제한(기관코드)",
    "공동도급",
    "지역제한",
    "원문",
    "첨부파일",
]
LEGACY_BID_NOTICE_SHEET_HEADERS = [
    "공고번호",
    "차수",
    "공고명",
    "수요기관",
    "업무구분",
    "조달구분",
    "게시일시",
    "마감일시",
    "사업금액",
    "기초금액",
    "지역제한",
    "공고링크",
]
EXPORT_CLAIM_MINUTES = 5


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _format_datetime(value: datetime | None) -> str:
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=KST)
    return value.astimezone(KST).strftime("%Y-%m-%d %H:%M")


def _format_notice_schedule(notice: ScraperNoticeModel) -> str:
    published_at = _format_datetime(notice.published_at)
    deadline_at = _format_datetime(notice.deadline_at)
    if not published_at and not deadline_at:
        return ""
    return f"{published_at}\n({deadline_at})"


def _notice_number(notice: ScraperNoticeModel) -> str:
    notice_no = notice.bid_notice_no or notice.notice_id or ""
    notice_ord = (notice.bid_notice_ord or "").strip()
    return f"{notice_no}-{notice_ord}" if notice_no and notice_ord else notice_no


def _attachment_urls(notice: ScraperNoticeModel) -> str:
    try:
        source = json.loads(notice.source_payload or "{}")
    except (TypeError, ValueError):
        return ""
    if not isinstance(source, dict):
        return ""

    urls: list[str] = []
    for index in range(1, 11):
        url = str(source.get(f"ntceSpecDocUrl{index}") or "").strip()
        if url and url not in urls:
            urls.append(url)
    return "\n".join(urls)


def _joint_supply_text(value: bool | None) -> str:
    if value is None:
        return "확인 필요"
    return "가능" if value else "불가"


def _notice_key(row: list[str | int | float]) -> str:
    return str(row[0]).strip()


def build_bid_notice_sheet_rows(
    notices: list[ScraperNoticeModel],
) -> list[list[str | int | float]]:
    return [
        [
            _notice_number(notice),
            notice.business_name or notice.title or "",
            _format_notice_schedule(notice),
            notice.demand_agency_name or notice.agency or "",
            float(notice.base_amount) if notice.base_amount is not None else "",
            notice.industry_restriction_codes or "",
            _joint_supply_text(notice.joint_supply_allowed),
            notice.region_restriction or "",
            notice.notice_url or "",
            _attachment_urls(notice),
        ]
        for notice in notices
    ]


def _migrate_legacy_rows(rows: list[list[Any]]) -> list[list[str]]:
    migrated_rows: list[list[str]] = []
    for row in rows:
        notice_no = str(row[0]).strip() if len(row) > 0 else ""
        if not notice_no:
            continue
        notice_ord = str(row[1]).strip() if len(row) > 1 else ""
        published_at = str(row[6]).strip() if len(row) > 6 else ""
        deadline_at = str(row[7]).strip() if len(row) > 7 else ""
        migrated_rows.append(
            [
                f"{notice_no}-{notice_ord}" if notice_ord else notice_no,
                str(row[2]).strip() if len(row) > 2 else "",
                f"{published_at}\n({deadline_at})" if published_at or deadline_at else "",
                str(row[3]).strip() if len(row) > 3 else "",
                str(row[8]).strip() if len(row) > 8 else "",
                "",
                "확인 필요",
                str(row[10]).strip() if len(row) > 10 else "",
                str(row[11]).strip() if len(row) > 11 else "",
                "",
            ]
        )
    return migrated_rows


def build_bid_notice_preview_token(
    *,
    destination: SheetDestinationModel,
    notice_ids: list[int],
    rows: list[list[str | int | float]],
) -> str:
    payload = json.dumps(
        {
            "destination_id": destination.id,
            "spreadsheet_id": destination.spreadsheet_id,
            "tab_name": destination.tab_name,
            "notice_ids": notice_ids,
            "headers": BID_NOTICE_SHEET_HEADERS,
            "rows": rows,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class BidNoticeSheetWriter(GoogleSheetWriter):
    def verify_connection(self):
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
            return spreadsheet_title, False, "NOT_CHECKED"

        escaped_tab = self.tab_name.replace("'", "''")
        response = (
            self.service.spreadsheets()
            .values()
            .get(
                spreadsheetId=self.spreadsheet_id,
                range=f"'{escaped_tab}'!A1:L1",
            )
            .execute()
        )
        values = response.get("values") or []
        if not values or not any(str(value).strip() for value in values[0]):
            status = "EMPTY"
        elif values[0][: len(BID_NOTICE_SHEET_HEADERS)] == BID_NOTICE_SHEET_HEADERS:
            status = "MATCH"
        elif values[0][: len(LEGACY_BID_NOTICE_SHEET_HEADERS)] == LEGACY_BID_NOTICE_SHEET_HEADERS:
            status = "MIGRATION_READY"
        else:
            status = "MISMATCH"
        return spreadsheet_title, True, status

    def upsert(self, rows: list[list[str | int | float]]) -> SheetUpsertResult:
        escaped_tab = self.tab_name.replace("'", "''")
        spreadsheets_api = self.service.spreadsheets()
        values_api = spreadsheets_api.values()
        header_response = values_api.get(
            spreadsheetId=self.spreadsheet_id,
            range=f"'{escaped_tab}'!A1:L1",
        ).execute()
        header_values = header_response.get("values") or []
        if not header_values or not any(str(value).strip() for value in header_values[0]):
            values_api.update(
                spreadsheetId=self.spreadsheet_id,
                range=f"'{escaped_tab}'!A1",
                valueInputOption="RAW",
                body={"values": [BID_NOTICE_SHEET_HEADERS]},
            ).execute()
        elif header_values[0][: len(LEGACY_BID_NOTICE_SHEET_HEADERS)] == LEGACY_BID_NOTICE_SHEET_HEADERS:
            legacy_rows = (
                values_api.get(
                    spreadsheetId=self.spreadsheet_id,
                    range=f"'{escaped_tab}'!A2:L",
                )
                .execute()
                .get("values")
                or []
            )
            existing_rows = _migrate_legacy_rows(legacy_rows)
            values_api.clear(
                spreadsheetId=self.spreadsheet_id,
                range=f"'{escaped_tab}'!A:L",
                body={},
            ).execute()
            values_api.update(
                spreadsheetId=self.spreadsheet_id,
                range=f"'{escaped_tab}'!A1",
                valueInputOption="RAW",
                body={"values": [BID_NOTICE_SHEET_HEADERS, *existing_rows]},
            ).execute()
        elif header_values[0][: len(BID_NOTICE_SHEET_HEADERS)] != BID_NOTICE_SHEET_HEADERS:
            raise SheetExportConfigurationError(
                "입찰공고 Sheet의 A:J 헤더가 새 10개 열과 일치하지 않습니다."
            )

        if header_values and header_values[0][: len(LEGACY_BID_NOTICE_SHEET_HEADERS)] == LEGACY_BID_NOTICE_SHEET_HEADERS:
            pass
        else:
            existing_rows = (
                values_api.get(
                    spreadsheetId=self.spreadsheet_id,
                    range=f"'{escaped_tab}'!A2:J",
                )
                .execute()
                .get("values")
                or []
            )
        row_number_by_key: dict[str, int] = {}
        for row_number, existing_row in enumerate(existing_rows, start=2):
            if not existing_row or not str(existing_row[0]).strip():
                continue
            key = str(existing_row[0]).strip()
            if key in row_number_by_key:
                raise SheetExportConfigurationError(
                    f"기존 Sheet에 공고번호 {key}가 중복되어 있습니다."
                )
            row_number_by_key[key] = row_number

        write_data: list[dict[str, Any]] = []
        inserted_count = 0
        next_row_number = len(existing_rows) + 2
        for row in rows:
            key = _notice_key(row)
            current_row_number = row_number_by_key.get(key)
            if current_row_number is None:
                current_row_number = next_row_number
                next_row_number += 1
                row_number_by_key[key] = current_row_number
                inserted_count += 1
            write_data.append(
                {
                    "range": f"'{escaped_tab}'!A{current_row_number}:J{current_row_number}",
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
                body={"valueInputOption": "RAW", "data": write_data},
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
                                    "startColumnIndex": 4,
                                    "endColumnIndex": 5,
                                },
                                "cell": {
                                    "userEnteredFormat": {
                                        "numberFormat": {"type": "NUMBER", "pattern": "#,##0"}
                                    }
                                },
                                "fields": "userEnteredFormat.numberFormat",
                            },
                        },
                        {
                            "repeatCell": {
                                "range": {
                                    "sheetId": sheet_id,
                                    "startColumnIndex": 2,
                                    "endColumnIndex": 3,
                                },
                                "cell": {
                                    "userEnteredFormat": {
                                        "wrapStrategy": "WRAP"
                                    }
                                },
                                "fields": "userEnteredFormat.wrapStrategy",
                            },
                        }
                    ]
                },
            ).execute()
        return SheetUpsertResult(
            inserted_count=inserted_count,
            updated_count=len(rows) - inserted_count,
        )


@dataclass(frozen=True)
class BidNoticeSheetExportClaim:
    lock_token: str
    export_ids: list[int]


def claim_bid_notice_sheet_exports(
    db: Session,
    *,
    destination: SheetDestinationModel,
    organization_id: int,
    user_id: int,
    notices: list[ScraperNoticeModel],
) -> BidNoticeSheetExportClaim:
    lock_token = str(uuid4())
    stale_before = _utcnow() - timedelta(minutes=EXPORT_CLAIM_MINUTES)
    locked = db.execute(
        update(SheetDestinationModel)
        .where(
            SheetDestinationModel.id == destination.id,
            SheetDestinationModel.organization_id == organization_id,
            SheetDestinationModel.owner_user_id == user_id,
            SheetDestinationModel.is_active.is_(True),
            or_(
                SheetDestinationModel.export_lock_token.is_(None),
                SheetDestinationModel.export_lock_claimed_at.is_(None),
                SheetDestinationModel.export_lock_claimed_at <= stale_before,
            ),
        )
        .values(export_lock_token=lock_token, export_lock_claimed_at=_utcnow())
    )
    if locked.rowcount != 1:
        db.rollback()
        raise SheetExportConflictError("같은 Sheet의 다른 반영 작업이 이미 진행 중입니다.")

    notice_ids = [notice.id for notice in notices]
    existing = (
        db.execute(
            select(BidNoticeSheetExportModel).where(
                BidNoticeSheetExportModel.destination_id == destination.id,
                BidNoticeSheetExportModel.notice_id.in_(notice_ids),
            )
        )
        .scalars()
        .all()
    )
    by_notice_id = {item.notice_id: item for item in existing}
    records: list[BidNoticeSheetExportModel] = []
    for notice in notices:
        record = by_notice_id.get(notice.id)
        if record is None:
            record = BidNoticeSheetExportModel(
                destination_id=destination.id,
                organization_id=organization_id,
                user_id=user_id,
                notice_id=notice.id,
            )
            db.add(record)
        else:
            record.organization_id = organization_id
            record.user_id = user_id
            record.status = "PENDING"
            record.attempt_count += 1
            record.error_message = None
            record.succeeded_at = None
        records.append(record)
    db.commit()
    for record in records:
        db.refresh(record)
    return BidNoticeSheetExportClaim(lock_token=lock_token, export_ids=[record.id for record in records])


def complete_bid_notice_sheet_exports(
    db: Session, *, claim: BidNoticeSheetExportClaim
) -> None:
    now = _utcnow()
    db.execute(
        update(SheetDestinationModel)
        .where(SheetDestinationModel.export_lock_token == claim.lock_token)
        .values(export_lock_token=None, export_lock_claimed_at=None)
    )
    db.execute(
        update(BidNoticeSheetExportModel)
        .where(BidNoticeSheetExportModel.id.in_(claim.export_ids))
        .values(status="SUCCEEDED", error_message=None, succeeded_at=now)
    )
    db.commit()


def fail_bid_notice_sheet_exports(
    db: Session, *, claim: BidNoticeSheetExportClaim, error_message: str
) -> None:
    db.rollback()
    db.execute(
        update(SheetDestinationModel)
        .where(SheetDestinationModel.export_lock_token == claim.lock_token)
        .values(export_lock_token=None, export_lock_claimed_at=None)
    )
    db.execute(
        update(BidNoticeSheetExportModel)
        .where(BidNoticeSheetExportModel.id.in_(claim.export_ids))
        .values(status="FAILED", error_message=error_message[:2000])
    )
    db.commit()
