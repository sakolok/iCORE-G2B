import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.g2b.opening_results.models import SheetDestinationModel
from app.g2b.opening_results.sheet_export import GoogleSheetWriter, SheetUpsertResult
from app.g2b.pre_specifications.models import (
    PreSpecificationModel,
    PreSpecificationSheetExportModel,
)
from app.g2b.pre_specifications.service import (
    deadline_status,
    mark_pre_specification_exported,
)


PRE_SPECIFICATION_TAB_NAME = "사전규격"
EXPORT_CLAIM_MINUTES = 15
SHEET_HEADERS = [
    "사전규격 등록번호",
    "사업명",
    "수요기관",
    "공고기관",
    "사업구분",
    "배정예산",
    "등록일",
    "의견마감일",
    "의견마감 상태",
    "담당자",
    "연락처",
    "첨부문서 URL",
]
LEGACY_SHEET_HEADERS = [
    "사전규격 등록번호",
    "사업명",
    "수요기관",
    "공고기관",
    "업무구분",
    "배정예산",
    "등록일시",
    "의견마감일시",
    "마감상태",
    "담당자",
    "연락처",
    "규격서 URL",
]


class PreSpecificationSheetError(RuntimeError):
    pass


class PreSpecificationSheetExportConflictError(RuntimeError):
    pass


@dataclass(frozen=True)
class PreSpecificationSheetClaimBatch:
    lock_token: str
    records: list[PreSpecificationSheetExportModel]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _sheet_datetime(value: datetime | None) -> str:
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=ZoneInfo("Asia/Seoul"))
    return value.astimezone(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d %H:%M")


def _sheet_number(value: Decimal | None) -> str | int | float:
    if value is None:
        return ""
    if value == value.to_integral_value():
        return int(value)
    return float(value)


def _attachment_urls(row: PreSpecificationModel) -> str:
    try:
        attachments = json.loads(row.attachments_json or "[]")
    except json.JSONDecodeError:
        return ""
    return "\n".join(
        url
        for item in attachments
        if isinstance(item, dict)
        and (url := str(item.get("url") or "").strip())
    )


def build_sheet_rows(
    rows: list[PreSpecificationModel],
) -> list[list[str | int | float]]:
    return [
        [
            row.bf_spec_rgst_no,
            row.business_name or "",
            row.demand_agency_name or "",
            row.ordering_agency_name or "",
            row.business_type or "",
            _sheet_number(row.allocated_budget),
            _sheet_datetime(row.registered_at),
            _sheet_datetime(row.opinion_deadline),
            deadline_status(row.opinion_deadline),
            row.contact_name or "",
            row.contact_phone or "",
            _attachment_urls(row),
        ]
        for row in rows
    ]


def build_sheet_preview_token(
    *,
    destination_id: int,
    spreadsheet_id: str,
    bf_spec_rgst_nos: list[str],
    rows: list[list[str | int | float]],
) -> str:
    payload = json.dumps(
        {
            "destination_id": destination_id,
            "spreadsheet_id": spreadsheet_id,
            "tab_name": PRE_SPECIFICATION_TAB_NAME,
            "bf_spec_rgst_nos": bf_spec_rgst_nos,
            "headers": SHEET_HEADERS,
            "rows": rows,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class PreSpecificationSheetWriter:
    def __init__(self, spreadsheet_id: str, service: Any) -> None:
        self.spreadsheet_id = spreadsheet_id.strip()
        if not self.spreadsheet_id:
            raise PreSpecificationSheetError("Google Spreadsheet ID가 필요합니다.")
        self.service = service

    @classmethod
    def from_env(cls, spreadsheet_id: str) -> "PreSpecificationSheetWriter":
        try:
            shared_writer = GoogleSheetWriter.from_env(
                spreadsheet_id=spreadsheet_id,
                tab_name=PRE_SPECIFICATION_TAB_NAME,
            )
        except Exception as error:
            raise PreSpecificationSheetError(str(error)) from error
        return cls(spreadsheet_id, shared_writer.service)

    def _ensure_tab(self) -> None:
        spreadsheets = self.service.spreadsheets()
        metadata = spreadsheets.get(
            spreadsheetId=self.spreadsheet_id,
            fields="sheets.properties.title",
        ).execute()
        titles = {
            str((sheet.get("properties") or {}).get("title") or "").strip()
            for sheet in metadata.get("sheets") or []
        }
        if PRE_SPECIFICATION_TAB_NAME not in titles:
            spreadsheets.batchUpdate(
                spreadsheetId=self.spreadsheet_id,
                body={
                    "requests": [
                        {
                            "addSheet": {
                                "properties": {"title": PRE_SPECIFICATION_TAB_NAME}
                            }
                        }
                    ]
                },
            ).execute()

    def upsert(
        self,
        rows: list[list[str | int | float]],
    ) -> SheetUpsertResult:
        self._ensure_tab()
        tab = PRE_SPECIFICATION_TAB_NAME.replace("'", "''")
        values = self.service.spreadsheets().values()
        header_response = values.get(
            spreadsheetId=self.spreadsheet_id,
            range=f"'{tab}'!A1:L1",
        ).execute()
        header = header_response.get("values") or []
        if not header or not any(str(value).strip() for value in header[0]):
            values.update(
                spreadsheetId=self.spreadsheet_id,
                range=f"'{tab}'!A1",
                valueInputOption="RAW",
                body={"values": [SHEET_HEADERS]},
            ).execute()
        elif header[0][: len(SHEET_HEADERS)] == LEGACY_SHEET_HEADERS:
            values.update(
                spreadsheetId=self.spreadsheet_id,
                range=f"'{tab}'!A1",
                valueInputOption="RAW",
                body={"values": [SHEET_HEADERS]},
            ).execute()
        elif header[0][: len(SHEET_HEADERS)] != SHEET_HEADERS:
            raise PreSpecificationSheetError(
                "사전규격 Sheet의 A:L 헤더가 고정 12개 열과 일치하지 않습니다."
            )

        existing_response = values.get(
            spreadsheetId=self.spreadsheet_id,
            range=f"'{tab}'!A2:L",
        ).execute()
        existing_rows = existing_response.get("values") or []
        row_number_by_id: dict[str, int] = {}
        for row_number, existing in enumerate(existing_rows, start=2):
            if not existing or not str(existing[0]).strip():
                continue
            item_id = str(existing[0]).strip()
            if item_id in row_number_by_id:
                raise PreSpecificationSheetError(
                    f"기존 Sheet에 사전규격 등록번호 {item_id}가 중복되어 있습니다."
                )
            row_number_by_id[item_id] = row_number

        write_data = []
        inserted_count = 0
        next_row_number = len(existing_rows) + 2
        for row in rows:
            item_id = str(row[0]).strip()
            row_number = row_number_by_id.get(item_id)
            if row_number is None:
                row_number = next_row_number
                next_row_number += 1
                inserted_count += 1
            write_data.append(
                {
                    "range": f"'{tab}'!A{row_number}:L{row_number}",
                    "values": [row],
                }
            )
        if write_data:
            values.batchUpdate(
                spreadsheetId=self.spreadsheet_id,
                body={"valueInputOption": "RAW", "data": write_data},
            ).execute()
        return SheetUpsertResult(
            inserted_count=inserted_count,
            updated_count=len(write_data) - inserted_count,
        )


def claim_sheet_exports(
    db: Session,
    *,
    destination: SheetDestinationModel,
    organization_id: int,
    user_id: int,
    rows: list[PreSpecificationModel],
) -> PreSpecificationSheetClaimBatch:
    now = _utcnow()
    stale_before = now - timedelta(minutes=EXPORT_CLAIM_MINUTES)
    lock_token = str(uuid4())
    target_filter = (
        SheetDestinationModel.spreadsheet_id == destination.spreadsheet_id,
        SheetDestinationModel.is_active.is_(True),
    )
    destination_count = db.scalar(
        select(func.count(SheetDestinationModel.id)).where(*target_filter)
    )
    locked = db.execute(
        update(SheetDestinationModel)
        .where(
            *target_filter,
            or_(
                SheetDestinationModel.export_lock_token.is_(None),
                SheetDestinationModel.export_lock_claimed_at.is_(None),
                SheetDestinationModel.export_lock_claimed_at <= stale_before,
            ),
        )
        .values(export_lock_token=lock_token, export_lock_claimed_at=now)
        .execution_options(synchronize_session=False)
    )
    if not destination_count or locked.rowcount != destination_count:
        db.rollback()
        raise PreSpecificationSheetExportConflictError(
            "같은 Google Sheet의 다른 반영 작업이 이미 진행 중입니다."
        )

    item_ids = [row.bf_spec_rgst_no for row in rows]
    physical_exports = db.execute(
        select(PreSpecificationSheetExportModel, SheetDestinationModel)
        .join(
            SheetDestinationModel,
            SheetDestinationModel.id
            == PreSpecificationSheetExportModel.destination_id,
        )
        .where(
            SheetDestinationModel.spreadsheet_id == destination.spreadsheet_id,
            PreSpecificationSheetExportModel.bf_spec_rgst_no.in_(item_ids),
        )
    ).all()
    for export, _ in physical_exports:
        if export.status == "SUCCEEDED":
            db.rollback()
            raise PreSpecificationSheetExportConflictError(
                "이미 이 Google Sheet에 반영된 사전규격이 포함되어 있습니다."
            )
        claimed_at = export.claimed_at
        if claimed_at.tzinfo is None:
            claimed_at = claimed_at.replace(tzinfo=timezone.utc)
        if export.status == "PENDING" and claimed_at > stale_before:
            db.rollback()
            raise PreSpecificationSheetExportConflictError(
                "같은 Sheet 반영 작업이 이미 진행 중입니다."
            )

    existing = {
        export.bf_spec_rgst_no: export
        for export, export_destination in physical_exports
        if export_destination.id == destination.id
    }
    claims = []
    for item_id in item_ids:
        claim = existing.get(item_id)
        if claim is None:
            claim = PreSpecificationSheetExportModel(
                destination_id=destination.id,
                organization_id=organization_id,
                bf_spec_rgst_no=item_id,
                exported_by_user_id=user_id,
                status="PENDING",
                claimed_at=now,
            )
            db.add(claim)
        else:
            claim.organization_id = organization_id
            claim.exported_by_user_id = user_id
            claim.status = "PENDING"
            claim.attempt_count += 1
            claim.error_message = None
            claim.claimed_at = now
            claim.succeeded_at = None
        claims.append(claim)
    try:
        db.commit()
    except IntegrityError as error:
        db.rollback()
        raise PreSpecificationSheetExportConflictError(
            "같은 Sheet 반영 요청이 동시에 처리되었습니다."
        ) from error
    return PreSpecificationSheetClaimBatch(lock_token=lock_token, records=claims)


def complete_sheet_exports(
    db: Session,
    *,
    claim_batch: PreSpecificationSheetClaimBatch,
    organization_id: int,
    user_id: int,
) -> None:
    now = _utcnow()
    released = db.execute(
        update(SheetDestinationModel)
        .where(SheetDestinationModel.export_lock_token == claim_batch.lock_token)
        .values(export_lock_token=None, export_lock_claimed_at=None)
        .execution_options(synchronize_session=False)
    )
    if released.rowcount < 1:
        db.rollback()
        raise PreSpecificationSheetExportConflictError(
            "Sheet 반영 잠금이 만료되었습니다. 다시 미리보기부터 진행하세요."
        )
    for claimed in claim_batch.records:
        claim = db.get(PreSpecificationSheetExportModel, claimed.id)
        claim.status = "SUCCEEDED"
        claim.succeeded_at = now
        claim.error_message = None
        mark_pre_specification_exported(
            db,
            organization_id=organization_id,
            user_id=user_id,
            bf_spec_rgst_no=claim.bf_spec_rgst_no,
        )
    db.commit()


def fail_sheet_exports(
    db: Session,
    *,
    claim_batch: PreSpecificationSheetClaimBatch,
    error_message: str,
) -> None:
    db.rollback()
    for claimed in claim_batch.records:
        claim = db.get(PreSpecificationSheetExportModel, claimed.id)
        if claim is None or claim.status == "SUCCEEDED":
            continue
        claim.status = "FAILED"
        claim.error_message = error_message[:2000]
    db.execute(
        update(SheetDestinationModel)
        .where(SheetDestinationModel.export_lock_token == claim_batch.lock_token)
        .values(export_lock_token=None, export_lock_claimed_at=None)
        .execution_options(synchronize_session=False)
    )
    db.commit()
