import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Iterable
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import exists, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.g2b.keyword_policy import evaluate_keyword_title, normalize_keywords
from app.g2b.opening_results.models import SheetDestinationModel
from app.g2b.pre_specifications.client import (
    PreSpecificationApiClient,
    PreSpecificationApiConfig,
    normalize_source_item,
)
from app.g2b.pre_specifications.models import (
    PreSpecificationCollectionRunModel,
    PreSpecificationModel,
    PreSpecificationSnapshotModel,
    PreSpecificationSheetExportModel,
    UserPreSpecificationProfileModel,
    UserPreSpecificationStateModel,
)
from app.g2b.pre_specifications.schemas import (
    PreSpecificationListQuery,
    PreSpecificationTransfer,
    date_window,
)


KST = ZoneInfo("Asia/Seoul")
ARCHIVE_RETENTION_DAYS = 14
class PreSpecificationAccessError(LookupError):
    pass


@dataclass(frozen=True)
class ArchivedPreSpecification:
    row: PreSpecificationModel
    handled_state: str
    handled_at: datetime
    can_restore: bool


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def get_user_pre_specification_profile(
    db: Session,
    *,
    organization_id: int,
    user_id: int,
    lock_for_update: bool = False,
) -> UserPreSpecificationProfileModel:
    statement = select(UserPreSpecificationProfileModel).where(
        UserPreSpecificationProfileModel.user_id == user_id
    )
    if lock_for_update:
        statement = statement.with_for_update()
    profile = db.execute(statement).scalar_one_or_none()
    if profile is None:
        profile = UserPreSpecificationProfileModel(
            organization_id=organization_id,
            user_id=user_id,
            enabled=False,
            keywords="",
            excluded_keywords="",
        )
        db.add(profile)
        db.flush()
    return profile


def update_user_pre_specification_profile(
    db: Session,
    *,
    organization_id: int,
    user_id: int,
    enabled: bool,
    keywords: list[str],
    excluded_keywords: list[str],
) -> UserPreSpecificationProfileModel:
    profile = get_user_pre_specification_profile(
        db,
        organization_id=organization_id,
        user_id=user_id,
        lock_for_update=True,
    )
    profile.organization_id = organization_id
    profile.enabled = enabled
    profile.keywords = ",".join(normalize_keywords(keywords))
    profile.excluded_keywords = ",".join(normalize_keywords(excluded_keywords))
    profile.updated_at = _utcnow()
    db.commit()
    db.refresh(profile)
    return profile


def _attachments(row: PreSpecificationModel) -> list[dict]:
    try:
        value = json.loads(row.attachments_json or "[]")
    except json.JSONDecodeError:
        return []
    return value if isinstance(value, list) else []


def deadline_status(value: datetime | None, now: datetime | None = None) -> str:
    if value is None:
        return "UNKNOWN"
    now_kst = (now or _utcnow()).astimezone(KST)
    deadline_kst = (
        value if value.tzinfo else value.replace(tzinfo=KST)
    ).astimezone(KST)
    if deadline_kst.date() == now_kst.date():
        return "TODAY"
    return "CLOSED" if deadline_kst < now_kst else "OPEN"


def _canonical_payload(raw: dict) -> tuple[str, str]:
    encoded = json.dumps(
        raw,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return encoded, hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _store_snapshot(
    db: Session,
    *,
    bf_spec_rgst_no: str,
    raw: dict,
) -> str:
    raw_payload, payload_hash = _canonical_payload(raw)
    snapshot = PreSpecificationSnapshotModel(
        bf_spec_rgst_no=bf_spec_rgst_no,
        payload_hash=payload_hash,
        raw_payload=raw_payload,
    )
    try:
        with db.begin_nested():
            db.add(snapshot)
            db.flush()
    except IntegrityError:
        pass
    return raw_payload


def upsert_pre_specifications(
    db: Session,
    items: Iterable[dict],
) -> tuple[int, int]:
    inserted = 0
    updated = 0
    now = _utcnow()
    field_names = (
        "bid_notice_no",
        "bid_notice_ord",
        "reference_no",
        "business_name",
        "business_type",
        "demand_agency_name",
        "ordering_agency_name",
        "allocated_budget",
        "registered_at",
        "opinion_deadline",
        "delivery_deadline",
        "delivery_deadline_text",
        "contact_name",
        "contact_phone",
    )

    for source_item in items:
        transfer = PreSpecificationTransfer.model_validate(source_item)
        row = db.get(PreSpecificationModel, transfer.bf_spec_rgst_no)
        if row is None:
            row = PreSpecificationModel(
                bf_spec_rgst_no=transfer.bf_spec_rgst_no,
                first_seen_at=now,
            )
            db.add(row)
            inserted += 1
        else:
            updated += 1

        for field_name in field_names:
            setattr(row, field_name, getattr(transfer, field_name))
        row.attachments_json = json.dumps(transfer.attachments, ensure_ascii=False)
        row.last_seen_at = now
        if transfer.raw:
            row.raw_payload = _store_snapshot(
                db,
                bf_spec_rgst_no=row.bf_spec_rgst_no,
                raw=transfer.raw,
            )
        else:
            row.raw_payload = "{}"

    db.commit()
    return inserted, updated


def collect_pre_specifications(
    db: Session,
    start_date: date,
    end_date: date,
    *,
    client: PreSpecificationApiClient | None = None,
) -> dict:
    start_at, end_at = date_window(start_date, end_date)
    run_key = (
        f"manual:{start_date.isoformat()}:{end_date.isoformat()}:{uuid4()}"
    )
    run = PreSpecificationCollectionRunModel(
        run_key=run_key,
        window_start=start_at,
        window_end=end_at,
    )
    db.add(run)
    db.commit()

    try:
        api_client = client or PreSpecificationApiClient(
            PreSpecificationApiConfig.from_env()
        )
        raw_rows = api_client.collect(start_date, end_date)
        normalized = [
            normalize_source_item(row)
            for row in raw_rows
            if str(row.get("bfSpecRgstNo") or "").strip()
        ]
        unique_rows = {
            row["bf_spec_rgst_no"]: row
            for row in normalized
        }
        inserted, updated = upsert_pre_specifications(
            db,
            unique_rows.values(),
        )
        run.fetched_count = len(unique_rows)
        run.inserted_count = inserted
        run.updated_count = updated
        run.status = "SUCCESS"
        run.finished_at = _utcnow()
        db.commit()
        return {
            "run_key": run_key,
            "fetched_count": len(unique_rows),
            "inserted_count": inserted,
            "updated_count": updated,
        }
    except Exception as error:
        db.rollback()
        run = db.get(PreSpecificationCollectionRunModel, run.id)
        run.status = "FAILED"
        run.error_message = str(error)[:2000]
        run.finished_at = _utcnow()
        db.commit()
        raise


def run_scheduled_pre_specifications(
    db: Session,
    *,
    now: datetime | None = None,
    client: PreSpecificationApiClient | None = None,
) -> dict:
    collection_date = (now or _utcnow()).astimezone(KST).date()
    return collect_pre_specifications(
        db,
        collection_date,
        collection_date,
        client=client,
    )


def _base_statement(query: PreSpecificationListQuery):
    statement = select(PreSpecificationModel)
    if query.registered_from or query.registered_to:
        start, end = date_window(
            query.registered_from or date(2000, 1, 1),
            query.registered_to or date(2100, 1, 1),
        )
        statement = statement.where(
            PreSpecificationModel.registered_at.between(start, end)
        )
    if query.q and query.q.strip():
        like = f"%{query.q.strip()}%"
        statement = statement.where(
            or_(
                PreSpecificationModel.bf_spec_rgst_no.like(like),
                PreSpecificationModel.business_name.like(like),
                PreSpecificationModel.demand_agency_name.like(like),
            )
        )
    if query.demand_agency and query.demand_agency.strip():
        statement = statement.where(
            PreSpecificationModel.demand_agency_name.like(
                f"%{query.demand_agency.strip()}%"
            )
        )
    if query.min_budget is not None:
        statement = statement.where(
            PreSpecificationModel.allocated_budget >= query.min_budget
        )
    if query.max_budget is not None:
        statement = statement.where(
            PreSpecificationModel.allocated_budget <= query.max_budget
        )
    if query.attachment == "HAS":
        statement = statement.where(PreSpecificationModel.attachments_json != "[]")
    elif query.attachment == "NONE":
        statement = statement.where(PreSpecificationModel.attachments_json == "[]")
    return statement


def _matches_keywords(row: PreSpecificationModel, query: PreSpecificationListQuery) -> bool:
    title = row.business_name or ""
    exclusion = evaluate_keyword_title(
        title,
        [],
        query.excluded_keywords,
    )
    if exclusion.excluded_keyword:
        return False
    if not query.keywords:
        return True

    matches = [
        evaluate_keyword_title(title, [keyword]).keep
        for keyword in query.keywords
    ]
    return all(matches) if query.keyword_mode == "AND" else any(matches)


def _matches_profile(
    row: PreSpecificationModel,
    profile: UserPreSpecificationProfileModel,
) -> bool:
    if not profile.enabled:
        return True
    return evaluate_keyword_title(
        row.business_name,
        profile.keywords,
        profile.excluded_keywords,
    ).keep


def list_pre_specifications(
    db: Session,
    query: PreSpecificationListQuery,
    *,
    organization_id: int,
    user_id: int,
) -> tuple[list[PreSpecificationModel], int]:
    profile = get_user_pre_specification_profile(
        db,
        organization_id=organization_id,
        user_id=user_id,
    )
    rows = db.scalars(
        _base_statement(query).where(
            *_visible_pre_specification_predicates(organization_id, user_id)
        ).order_by(
            PreSpecificationModel.registered_at.desc(),
            PreSpecificationModel.bf_spec_rgst_no.desc(),
        )
    ).all()
    now = _utcnow()
    filtered = [
        row
        for row in rows
        if _matches_profile(row, profile)
        and _matches_keywords(row, query)
        and (
            query.deadline_status == "ALL"
            or deadline_status(row.opinion_deadline, now) == query.deadline_status
        )
    ]
    total = len(filtered)
    start = (query.page - 1) * query.page_size
    return filtered[start : start + query.page_size], total


def _visible_pre_specification_predicates(
    organization_id: int,
    user_id: int,
) -> tuple:
    dismissed = exists(
        select(UserPreSpecificationStateModel.id).where(
            UserPreSpecificationStateModel.organization_id == organization_id,
            UserPreSpecificationStateModel.user_id == user_id,
            UserPreSpecificationStateModel.bf_spec_rgst_no
            == PreSpecificationModel.bf_spec_rgst_no,
            UserPreSpecificationStateModel.state == "DISMISSED",
        )
    )
    exported_to_active_personal_sheet = exists(
        select(PreSpecificationSheetExportModel.id)
        .join(
            SheetDestinationModel,
            SheetDestinationModel.id
            == PreSpecificationSheetExportModel.destination_id,
        )
        .where(
            PreSpecificationSheetExportModel.organization_id == organization_id,
            PreSpecificationSheetExportModel.bf_spec_rgst_no
            == PreSpecificationModel.bf_spec_rgst_no,
            PreSpecificationSheetExportModel.exported_by_user_id == user_id,
            PreSpecificationSheetExportModel.status == "SUCCEEDED",
            SheetDestinationModel.organization_id == organization_id,
            SheetDestinationModel.owner_user_id == user_id,
            SheetDestinationModel.is_active.is_(True),
        )
    )
    return (~dismissed, ~exported_to_active_personal_sheet)


def get_visible_pre_specification(
    db: Session,
    *,
    organization_id: int,
    user_id: int,
    bf_spec_rgst_no: str,
) -> PreSpecificationModel | None:
    row = db.scalar(
        select(PreSpecificationModel).where(
            PreSpecificationModel.bf_spec_rgst_no == bf_spec_rgst_no,
            *_visible_pre_specification_predicates(organization_id, user_id),
        )
    )
    if row is None:
        return None
    profile = get_user_pre_specification_profile(
        db,
        organization_id=organization_id,
        user_id=user_id,
    )
    return row if _matches_profile(row, profile) else None


def load_visible_pre_specifications(
    db: Session,
    *,
    organization_id: int,
    user_id: int,
    bf_spec_rgst_nos: Iterable[str],
) -> list[PreSpecificationModel]:
    requested = list(dict.fromkeys(bf_spec_rgst_nos))
    rows = db.scalars(
        select(PreSpecificationModel).where(
            PreSpecificationModel.bf_spec_rgst_no.in_(requested),
            *_visible_pre_specification_predicates(organization_id, user_id),
        )
    ).all()
    profile = get_user_pre_specification_profile(
        db,
        organization_id=organization_id,
        user_id=user_id,
    )
    by_id = {
        row.bf_spec_rgst_no: row
        for row in rows
        if _matches_profile(row, profile)
    }
    missing = [item_id for item_id in requested if item_id not in by_id]
    if missing:
        raise PreSpecificationAccessError(",".join(missing))
    return [by_id[item_id] for item_id in requested]


def _set_user_state(
    db: Session,
    *,
    organization_id: int,
    user_id: int,
    bf_spec_rgst_no: str,
    state: str,
) -> None:
    row = db.scalar(
        select(UserPreSpecificationStateModel).where(
            UserPreSpecificationStateModel.user_id == user_id,
            UserPreSpecificationStateModel.bf_spec_rgst_no == bf_spec_rgst_no,
        )
    )
    if row is None:
        db.add(
            UserPreSpecificationStateModel(
                organization_id=organization_id,
                user_id=user_id,
                bf_spec_rgst_no=bf_spec_rgst_no,
                state=state,
            )
        )
    else:
        row.organization_id = organization_id
        row.state = state
        row.acted_at = _utcnow()


def mark_pre_specification_exported(
    db: Session,
    *,
    organization_id: int,
    user_id: int,
    bf_spec_rgst_no: str,
) -> None:
    _set_user_state(
        db,
        organization_id=organization_id,
        user_id=user_id,
        bf_spec_rgst_no=bf_spec_rgst_no,
        state="EXPORTED",
    )


def dismiss_pre_specification(
    db: Session,
    *,
    organization_id: int,
    user_id: int,
    bf_spec_rgst_no: str,
) -> None:
    row = get_visible_pre_specification(
        db,
        organization_id=organization_id,
        user_id=user_id,
        bf_spec_rgst_no=bf_spec_rgst_no,
    )
    if row is None:
        raise PreSpecificationAccessError(bf_spec_rgst_no)
    _set_user_state(
        db,
        organization_id=organization_id,
        user_id=user_id,
        bf_spec_rgst_no=bf_spec_rgst_no,
        state="DISMISSED",
    )
    db.commit()


def list_archived_pre_specifications(
    db: Session,
    *,
    organization_id: int,
    user_id: int,
    page: int = 1,
    page_size: int = 30,
    bf_spec_rgst_no: str | None = None,
    now: datetime | None = None,
) -> tuple[list[ArchivedPreSpecification], int]:
    current = _as_utc(now) if now is not None else _utcnow()
    cutoff = current - timedelta(days=ARCHIVE_RETENTION_DAYS)
    archived: dict[str, ArchivedPreSpecification] = {}

    state_statement = (
        select(PreSpecificationModel, UserPreSpecificationStateModel)
        .join(
            UserPreSpecificationStateModel,
            UserPreSpecificationStateModel.bf_spec_rgst_no
            == PreSpecificationModel.bf_spec_rgst_no,
        )
        .where(
            UserPreSpecificationStateModel.organization_id == organization_id,
            UserPreSpecificationStateModel.user_id == user_id,
            UserPreSpecificationStateModel.state == "DISMISSED",
            UserPreSpecificationStateModel.acted_at >= cutoff,
        )
    )
    if bf_spec_rgst_no is not None:
        state_statement = state_statement.where(
            PreSpecificationModel.bf_spec_rgst_no == bf_spec_rgst_no
        )
    for source, state in db.execute(state_statement).all():
        handled_at = _as_utc(state.acted_at)
        archived[source.bf_spec_rgst_no] = ArchivedPreSpecification(
            row=source,
            handled_state=state.state,
            handled_at=handled_at,
            can_restore=state.state == "DISMISSED",
        )

    export_statement = (
        select(PreSpecificationModel, PreSpecificationSheetExportModel)
        .join(
            PreSpecificationSheetExportModel,
            PreSpecificationSheetExportModel.bf_spec_rgst_no
            == PreSpecificationModel.bf_spec_rgst_no,
        )
        .join(
            SheetDestinationModel,
            SheetDestinationModel.id
            == PreSpecificationSheetExportModel.destination_id,
        )
        .where(
            PreSpecificationSheetExportModel.organization_id == organization_id,
            PreSpecificationSheetExportModel.status == "SUCCEEDED",
            PreSpecificationSheetExportModel.succeeded_at.is_not(None),
            PreSpecificationSheetExportModel.succeeded_at >= cutoff,
            PreSpecificationSheetExportModel.exported_by_user_id == user_id,
            SheetDestinationModel.organization_id == organization_id,
            SheetDestinationModel.owner_user_id == user_id,
            SheetDestinationModel.is_active.is_(True),
        )
    )
    if bf_spec_rgst_no is not None:
        export_statement = export_statement.where(
            PreSpecificationModel.bf_spec_rgst_no == bf_spec_rgst_no
        )
    for source, export in db.execute(export_statement).all():
        handled_at = _as_utc(export.succeeded_at)
        existing = archived.get(source.bf_spec_rgst_no)
        if existing is None or handled_at > existing.handled_at:
            archived[source.bf_spec_rgst_no] = ArchivedPreSpecification(
                row=source,
                handled_state="EXPORTED",
                handled_at=handled_at,
                can_restore=False,
            )

    ordered = sorted(
        archived.values(),
        key=lambda item: (item.handled_at, item.row.bf_spec_rgst_no),
        reverse=True,
    )
    total = len(ordered)
    start = (page - 1) * page_size
    return ordered[start : start + page_size], total


def restore_dismissed_pre_specification(
    db: Session,
    *,
    organization_id: int,
    user_id: int,
    bf_spec_rgst_no: str,
    now: datetime | None = None,
) -> bool:
    state = db.scalar(
        select(UserPreSpecificationStateModel).where(
            UserPreSpecificationStateModel.organization_id == organization_id,
            UserPreSpecificationStateModel.user_id == user_id,
            UserPreSpecificationStateModel.bf_spec_rgst_no == bf_spec_rgst_no,
            UserPreSpecificationStateModel.state == "DISMISSED",
        )
    )
    current = _as_utc(now) if now is not None else _utcnow()
    if state is None or _as_utc(state.acted_at) < current - timedelta(
        days=ARCHIVE_RETENTION_DAYS
    ):
        raise PreSpecificationAccessError(bf_spec_rgst_no)
    db.delete(state)
    db.commit()
    return get_visible_pre_specification(
        db,
        organization_id=organization_id,
        user_id=user_id,
        bf_spec_rgst_no=bf_spec_rgst_no,
    ) is not None


def response_payload(row: PreSpecificationModel) -> dict:
    return {
        "bf_spec_rgst_no": row.bf_spec_rgst_no,
        "bid_notice_no": row.bid_notice_no,
        "bid_notice_ord": row.bid_notice_ord,
        "reference_no": row.reference_no,
        "business_name": row.business_name,
        "business_type": row.business_type,
        "demand_agency_name": row.demand_agency_name,
        "ordering_agency_name": row.ordering_agency_name,
        "allocated_budget": row.allocated_budget,
        "registered_at": row.registered_at,
        "opinion_deadline": row.opinion_deadline,
        "delivery_deadline": row.delivery_deadline,
        "delivery_deadline_text": row.delivery_deadline_text,
        "contact_name": row.contact_name,
        "contact_phone": row.contact_phone,
        "attachments": _attachments(row),
        "deadline_status": deadline_status(row.opinion_deadline),
        "first_seen_at": row.first_seen_at,
        "last_seen_at": row.last_seen_at,
    }
