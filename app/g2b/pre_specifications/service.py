import hashlib
import json
from datetime import date, datetime, timezone
from typing import Iterable
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.g2b.keyword_policy import evaluate_keyword_title
from app.g2b.pre_specifications.client import PreSpecificationApiClient, PreSpecificationApiConfig, normalize_source_item
from app.g2b.pre_specifications.models import (
    PreSpecificationCollectionRunModel,
    PreSpecificationModel,
    PreSpecificationSnapshotModel,
    UserPreSpecificationStateModel,
)
from app.g2b.pre_specifications.schemas import PreSpecificationListQuery, PreSpecificationTransfer, date_window


KST = ZoneInfo("Asia/Seoul")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


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
    deadline_kst = (value if value.tzinfo else value.replace(tzinfo=KST)).astimezone(KST)
    if deadline_kst.date() == now_kst.date():
        return "TODAY"
    return "CLOSED" if deadline_kst < now_kst else "OPEN"


def _payload_hash(raw: dict) -> str:
    encoded = json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def upsert_pre_specifications(db: Session, items: Iterable[dict]) -> tuple[int, int]:
    inserted = updated = 0
    now = _utcnow()
    for source_item in items:
        transfer = PreSpecificationTransfer.model_validate(source_item)
        row = db.get(PreSpecificationModel, transfer.bf_spec_rgst_no)
        if row is None:
            row = PreSpecificationModel(bf_spec_rgst_no=transfer.bf_spec_rgst_no, first_seen_at=now)
            db.add(row)
            inserted += 1
        else:
            updated += 1
        for name in ("bid_notice_no", "bid_notice_ord", "reference_no", "business_name", "business_type", "demand_agency_name", "ordering_agency_name", "allocated_budget", "registered_at", "opinion_deadline", "delivery_deadline", "contact_name", "contact_phone"):
            setattr(row, name, getattr(transfer, name))
        row.attachments_json = json.dumps(transfer.attachments, ensure_ascii=False)
        row.raw_payload = json.dumps(transfer.raw, ensure_ascii=False, sort_keys=True)
        row.last_seen_at = now
        if transfer.raw:
            snapshot = PreSpecificationSnapshotModel(bf_spec_rgst_no=row.bf_spec_rgst_no, payload_hash=_payload_hash(transfer.raw), raw_payload=row.raw_payload)
            try:
                with db.begin_nested():
                    db.add(snapshot)
                    db.flush()
            except IntegrityError:
                pass
    db.commit()
    return inserted, updated


def collect_pre_specifications(db: Session, start_date: date, end_date: date) -> dict:
    start_at, end_at = date_window(start_date, end_date)
    run_key = f"manual:{start_date.isoformat()}:{end_date.isoformat()}:{_utcnow().strftime('%Y%m%d%H%M%S%f')}"
    run = PreSpecificationCollectionRunModel(run_key=run_key, window_start=start_at, window_end=end_at)
    db.add(run)
    db.commit()
    try:
        raw_rows = PreSpecificationApiClient(PreSpecificationApiConfig.from_env()).collect(start_date, end_date)
        normalized = [normalize_source_item(row) for row in raw_rows if str(row.get("bfSpecRgstNo") or "").strip()]
        unique = {row["bf_spec_rgst_no"]: row for row in normalized}
        inserted, updated = upsert_pre_specifications(db, unique.values())
        run.fetched_count, run.inserted_count, run.updated_count = len(unique), inserted, updated
        run.status, run.finished_at = "SUCCESS", _utcnow()
        db.commit()
        return {"run_key": run_key, "fetched_count": len(unique), "inserted_count": inserted, "updated_count": updated}
    except Exception as error:
        run.status, run.error_message, run.finished_at = "FAILED", str(error), _utcnow()
        db.commit()
        raise


def run_scheduled_pre_specifications(db: Session) -> dict:
    today = _utcnow().astimezone(KST).date()
    return collect_pre_specifications(db, today, today)


def _base_statement(query: PreSpecificationListQuery):
    statement = select(PreSpecificationModel)
    if query.registered_from or query.registered_to:
        start, end = date_window(query.registered_from or date(2000, 1, 1), query.registered_to or date(2100, 1, 1))
        statement = statement.where(PreSpecificationModel.registered_at.between(start, end))
    if query.q:
        like = f"%{query.q.strip()}%"
        statement = statement.where(or_(PreSpecificationModel.bf_spec_rgst_no.like(like), PreSpecificationModel.business_name.like(like), PreSpecificationModel.demand_agency_name.like(like)))
    if query.demand_agency:
        statement = statement.where(PreSpecificationModel.demand_agency_name.like(f"%{query.demand_agency.strip()}%"))
    if query.min_budget is not None:
        statement = statement.where(PreSpecificationModel.allocated_budget >= query.min_budget)
    if query.max_budget is not None:
        statement = statement.where(PreSpecificationModel.allocated_budget <= query.max_budget)
    if query.attachment == "HAS":
        statement = statement.where(PreSpecificationModel.attachments_json != "[]")
    elif query.attachment == "NONE":
        statement = statement.where(PreSpecificationModel.attachments_json == "[]")
    return statement


def _user_states(db: Session, *, organization_id: int, user_id: int) -> dict[str, str]:
    return {
        row.bf_spec_rgst_no: row.state
        for row in db.scalars(
            select(UserPreSpecificationStateModel).where(
                UserPreSpecificationStateModel.organization_id == organization_id,
                UserPreSpecificationStateModel.user_id == user_id,
            )
        ).all()
    }


def _filter_pre_specifications(rows: Iterable[PreSpecificationModel], query: PreSpecificationListQuery, states: dict[str, str], *, archived_only: bool) -> list[PreSpecificationModel]:
    filtered: list[PreSpecificationModel] = []
    now = _utcnow()
    for row in rows:
        state = states.get(row.bf_spec_rgst_no)
        if archived_only:
            if state != "ARCHIVED":
                continue
        elif state == "ARCHIVED" or (not query.include_exported and state == "EXPORTED"):
            continue
        title = row.business_name or ""
        if query.keywords:
            matches = [evaluate_keyword_title(title, [word], query.excluded_keywords).keep for word in query.keywords]
            if (query.keyword_mode == "AND" and not all(matches)) or (query.keyword_mode == "OR" and not any(matches)):
                continue
        elif not evaluate_keyword_title(title, [], query.excluded_keywords).keep and query.excluded_keywords:
            continue
        if query.deadline_status != "ALL" and deadline_status(row.opinion_deadline, now) != query.deadline_status:
            continue
        filtered.append(row)
    return filtered


def list_pre_specifications(db: Session, query: PreSpecificationListQuery, *, organization_id: int, user_id: int) -> tuple[list[PreSpecificationModel], int, set[str]]:
    statement = _base_statement(query)
    states = _user_states(db, organization_id=organization_id, user_id=user_id)
    rows = db.scalars(statement.order_by(PreSpecificationModel.registered_at.desc(), PreSpecificationModel.bf_spec_rgst_no.desc())).all()
    filtered = _filter_pre_specifications(rows, query, states, archived_only=False)
    total = len(filtered)
    start = (query.page - 1) * query.page_size
    return filtered[start:start + query.page_size], total, {key for key, state in states.items() if state == "EXPORTED"}


def list_archived_pre_specifications(db: Session, query: PreSpecificationListQuery, *, organization_id: int, user_id: int) -> tuple[list[PreSpecificationModel], int]:
    statement = _base_statement(query)
    states = _user_states(db, organization_id=organization_id, user_id=user_id)
    rows = db.scalars(statement.order_by(PreSpecificationModel.registered_at.desc(), PreSpecificationModel.bf_spec_rgst_no.desc())).all()
    filtered = _filter_pre_specifications(rows, query, states, archived_only=True)
    total = len(filtered)
    start = (query.page - 1) * query.page_size
    return filtered[start:start + query.page_size], total


def get_pre_specification(db: Session, bf_spec_rgst_no: str) -> PreSpecificationModel | None:
    return db.get(PreSpecificationModel, bf_spec_rgst_no)


def response_payload(row: PreSpecificationModel, *, exported: bool = False) -> dict:
    return {"bf_spec_rgst_no": row.bf_spec_rgst_no, "bid_notice_no": row.bid_notice_no, "bid_notice_ord": row.bid_notice_ord, "reference_no": row.reference_no, "business_name": row.business_name, "business_type": row.business_type, "demand_agency_name": row.demand_agency_name, "ordering_agency_name": row.ordering_agency_name, "allocated_budget": row.allocated_budget, "registered_at": row.registered_at, "opinion_deadline": row.opinion_deadline, "delivery_deadline": row.delivery_deadline, "contact_name": row.contact_name, "contact_phone": row.contact_phone, "attachments": _attachments(row), "deadline_status": deadline_status(row.opinion_deadline), "exported": exported, "first_seen_at": row.first_seen_at, "last_seen_at": row.last_seen_at}


def _set_user_state(db: Session, *, organization_id: int, user_id: int, ids: Iterable[str], state: str) -> None:
    now = _utcnow()
    for item_id in ids:
        row = db.scalar(
            select(UserPreSpecificationStateModel).where(
                UserPreSpecificationStateModel.organization_id == organization_id,
                UserPreSpecificationStateModel.user_id == user_id,
                UserPreSpecificationStateModel.bf_spec_rgst_no == item_id,
            )
        )
        if row is None:
            db.add(UserPreSpecificationStateModel(organization_id=organization_id, user_id=user_id, bf_spec_rgst_no=item_id, state=state, acted_at=now))
        else:
            row.state, row.acted_at = state, now
    db.commit()


def mark_exported(db: Session, *, organization_id: int, user_id: int, ids: Iterable[str]) -> None:
    _set_user_state(db, organization_id=organization_id, user_id=user_id, ids=ids, state="EXPORTED")


def restore_removed_sheet_pre_specifications(
    db: Session,
    *,
    organization_id: int,
    user_id: int,
    sheet_ids: Iterable[str],
) -> int:
    """Return exported items to the work list when they no longer exist in the synced Sheet."""
    present_ids = set(sheet_ids)
    rows = db.scalars(
        select(UserPreSpecificationStateModel).where(
            UserPreSpecificationStateModel.organization_id == organization_id,
            UserPreSpecificationStateModel.user_id == user_id,
            UserPreSpecificationStateModel.state == "EXPORTED",
        )
    ).all()
    removed_rows = [row for row in rows if row.bf_spec_rgst_no not in present_ids]
    for row in removed_rows:
        db.delete(row)
    db.commit()
    return len(removed_rows)


def archive_pre_specifications(db: Session, *, organization_id: int, user_id: int, ids: Iterable[str]) -> tuple[int, list[str]]:
    requested = list(dict.fromkeys(ids))
    existing = set(db.scalars(select(PreSpecificationModel.bf_spec_rgst_no).where(PreSpecificationModel.bf_spec_rgst_no.in_(requested))).all())
    selected = [item_id for item_id in requested if item_id in existing]
    _set_user_state(db, organization_id=organization_id, user_id=user_id, ids=selected, state="ARCHIVED")
    return len(selected), [item_id for item_id in requested if item_id not in existing]


def restore_archived_pre_specifications(db: Session, *, organization_id: int, user_id: int, ids: Iterable[str]) -> tuple[int, list[str]]:
    requested = list(dict.fromkeys(ids))
    rows = db.scalars(
        select(UserPreSpecificationStateModel).where(
            UserPreSpecificationStateModel.organization_id == organization_id,
            UserPreSpecificationStateModel.user_id == user_id,
            UserPreSpecificationStateModel.bf_spec_rgst_no.in_(requested),
            UserPreSpecificationStateModel.state == "ARCHIVED",
        )
    ).all()
    restored = {row.bf_spec_rgst_no for row in rows}
    for row in rows:
        db.delete(row)
    db.commit()
    return len(restored), [item_id for item_id in requested if item_id not in restored]
