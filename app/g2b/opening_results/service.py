import hashlib
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import exists, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.data.models import ScraperNoticeModel
from app.g2b.bid_notice import canonical_bid_notice_order
from app.g2b.bid_notices.service import (
    enrich_bid_notice_contexts_for_opening_rounds,
)
from app.g2b.keyword_policy import normalize_keywords
from app.g2b.opening_results.client import OpeningResultApiClient, OpeningResultApiConfig
from app.g2b.opening_results.models import (
    BidOpeningCollectionLeaseModel,
    BidOpeningCollectionRunModel,
    BidOpeningEntryModel,
    BidOpeningRoundModel,
    BidResultSnapshotModel,
    UserOpeningResultMatchModel,
)
from app.g2b.opening_results.matching import (
    get_visible_result_match,
    sync_organization_matches,
    sync_user_matches,
    visible_result_predicates,
)
from app.g2b.opening_results.schemas import (
    CollectOpeningResultsRequest,
    CollectOpeningResultsResponse,
    OpeningResultListQuery,
    OpeningStatus,
    ScheduledCollectOpeningResultsResponse,
)


KST = ZoneInfo("Asia/Seoul")
COLLECTION_SLOT_HOURS = (8, 11, 14, 17)
COLLECTION_LEASE_MINUTES = 45
FRONT_LIST_DAYS = 14


class OpeningResultCollectionLeaseLostError(RuntimeError):
    pass


class OpeningResultCollectionConflictError(RuntimeError):
    pass


def _claim_global_collection_lease(db: Session, business_type: str) -> str:
    now = datetime.now(timezone.utc)
    stale_before = now - timedelta(minutes=COLLECTION_LEASE_MINUTES)
    claim_token = str(uuid4())
    lease = db.get(BidOpeningCollectionLeaseModel, business_type)
    if lease is None:
        db.add(
            BidOpeningCollectionLeaseModel(
                business_type=business_type,
                claim_token=claim_token,
                claimed_at=now,
            )
        )
        try:
            db.commit()
            return claim_token
        except IntegrityError:
            db.rollback()

    claimed = db.execute(
        update(BidOpeningCollectionLeaseModel)
        .where(
            BidOpeningCollectionLeaseModel.business_type == business_type,
            or_(
                BidOpeningCollectionLeaseModel.claim_token.is_(None),
                BidOpeningCollectionLeaseModel.claimed_at.is_(None),
                BidOpeningCollectionLeaseModel.claimed_at <= stale_before,
            ),
        )
        .values(claim_token=claim_token, claimed_at=now)
        .execution_options(synchronize_session=False)
    )
    if claimed.rowcount != 1:
        db.rollback()
        raise OpeningResultCollectionConflictError(
            "같은 사업유형의 개찰결과 수집이 이미 진행 중입니다."
        )
    db.commit()
    return claim_token


def _release_global_collection_lease(
    db: Session,
    *,
    business_type: str,
    global_claim_token: str,
) -> None:
    db.execute(
        update(BidOpeningCollectionLeaseModel)
        .where(
            BidOpeningCollectionLeaseModel.business_type == business_type,
            BidOpeningCollectionLeaseModel.claim_token == global_claim_token,
        )
        .values(claim_token=None, claimed_at=None)
        .execution_options(synchronize_session=False)
    )
    db.commit()


def _refresh_collection_lease(
    db: Session,
    *,
    collection_run_id: int | None,
    collection_claim_token: str | None,
    business_type: str,
    global_claim_token: str,
) -> None:
    if collection_run_id is not None and collection_claim_token is not None:
        refreshed_run = db.execute(
            update(BidOpeningCollectionRunModel)
            .where(
                BidOpeningCollectionRunModel.id == collection_run_id,
                BidOpeningCollectionRunModel.claim_token == collection_claim_token,
            )
            .values(started_at=datetime.now(timezone.utc))
            .execution_options(synchronize_session=False)
        )
        if refreshed_run.rowcount != 1:
            raise OpeningResultCollectionLeaseLostError(
                "개찰결과 수집 작업의 소유권이 만료되었습니다."
            )
    refreshed_global = db.execute(
        update(BidOpeningCollectionLeaseModel)
        .where(
            BidOpeningCollectionLeaseModel.business_type == business_type,
            BidOpeningCollectionLeaseModel.claim_token == global_claim_token,
        )
        .values(claimed_at=datetime.now(timezone.utc))
        .execution_options(synchronize_session=False)
    )
    if refreshed_global.rowcount != 1:
        raise OpeningResultCollectionLeaseLostError(
            "개찰결과 공통 수집 잠금이 만료되었습니다."
        )


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _canonical_key_code(value: Any) -> str:
    cleaned = _clean(value)
    if cleaned.isdigit():
        return str(int(cleaned))
    return cleaned or "0"


def _parse_int(value: Any) -> int | None:
    cleaned = _clean(value).replace(",", "")
    if not cleaned:
        return None
    try:
        return int(cleaned)
    except ValueError:
        return None


def _parse_decimal(value: Any) -> Decimal | None:
    cleaned = _clean(value).replace(",", "").replace("%", "")
    if not cleaned:
        return None
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def _parse_datetime(value: Any) -> datetime | None:
    cleaned = _clean(value)
    if not cleaned:
        return None
    normalized = cleaned.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        parsed = None
    if parsed is None:
        for fmt in (
            "%Y%m%d%H%M%S",
            "%Y%m%d%H%M",
            "%Y%m%d",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
        ):
            try:
                parsed = datetime.strptime(cleaned, fmt)
                break
            except ValueError:
                continue
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=KST)
    return parsed.astimezone(timezone.utc)


def _canonical_payload(payload: dict[str, Any]) -> tuple[str, str]:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return raw, hashlib.sha256(raw.encode("utf-8")).hexdigest()


def normalize_status(label: str) -> OpeningStatus:
    if "취소" in label:
        return OpeningStatus.CANCELLED
    if "유찰" in label:
        return OpeningStatus.FAILED
    if "재입찰" in label:
        return OpeningStatus.REBID
    if "최종낙찰" in label or "낙찰" in label:
        return OpeningStatus.AWARDED
    if "개찰완료" in label or "개찰" in label:
        return OpeningStatus.OPENED
    return OpeningStatus.UNKNOWN


def build_round_external_key(item: dict[str, Any], business_type: str) -> str:
    bid_notice_no = _clean(item.get("bidNtceNo"))
    if not bid_notice_no:
        raise ValueError("bidNtceNo가 없는 개찰결과는 저장할 수 없습니다.")
    return "|".join(
        [
            business_type,
            bid_notice_no,
            canonical_bid_notice_order(item.get("bidNtceOrd")),
            _canonical_key_code(item.get("bidClsfcNo")),
            _canonical_key_code(item.get("rbidNo")),
        ]
    )


def _entry_external_key(item: dict[str, Any], round_external_key: str) -> str:
    identity = _clean(item.get("prcbdrBizno")) or _clean(item.get("prcbdrNm"))
    if not identity:
        identity = f"rank:{_clean(item.get('opengRank')) or 'unknown'}"
    raw = f"{round_external_key}|{identity}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _store_snapshot(
    db: Session,
    *,
    entity_type: str,
    entity_key: str,
    payload: dict[str, Any],
) -> None:
    raw_payload, payload_hash = _canonical_payload(payload)
    exists = db.execute(
        select(BidResultSnapshotModel.id).where(
            BidResultSnapshotModel.entity_type == entity_type,
            BidResultSnapshotModel.entity_key == entity_key,
            BidResultSnapshotModel.payload_hash == payload_hash,
        )
    ).scalar_one_or_none()
    if exists is None:
        db.add(
            BidResultSnapshotModel(
                entity_type=entity_type,
                entity_key=entity_key,
                payload_hash=payload_hash,
                raw_payload=raw_payload,
            )
        )


def _winner_index(
    winners: list[dict[str, Any]], business_type: str
) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    for winner in winners:
        try:
            indexed[build_round_external_key(winner, business_type)] = winner
        except ValueError:
            continue
    return indexed


def _apply_winner(
    db: Session,
    row: BidOpeningRoundModel,
    winner: dict[str, Any],
) -> None:
    row.winner_business_no = _clean(winner.get("bidwinnrBizno")) or None
    row.winner_company_name = _clean(winner.get("bidwinnrNm")) or None
    row.winning_amount = _parse_decimal(winner.get("sucsfbidAmt"))
    row.winning_rate = _parse_decimal(winner.get("sucsfbidRate"))
    row.final_awarded_at = _parse_datetime(
        winner.get("fnlSucsfDate") or winner.get("rlOpengDt")
    )
    row.status = OpeningStatus.AWARDED.value

    entries = db.execute(
        select(BidOpeningEntryModel).where(BidOpeningEntryModel.round_id == row.id)
    ).scalars()
    for entry in entries:
        entry.is_winner = bool(
            (
                row.winner_business_no
                and entry.business_no == row.winner_business_no
            )
            or (
                row.winner_company_name
                and entry.company_name == row.winner_company_name
            )
        )


def _upsert_round(
    db: Session,
    summary: dict[str, Any],
    winner: dict[str, Any] | None,
    business_type: str,
) -> tuple[BidOpeningRoundModel, bool]:
    external_key = build_round_external_key(summary, business_type)
    row = db.execute(
        select(BidOpeningRoundModel).where(BidOpeningRoundModel.external_key == external_key)
    ).scalar_one_or_none()
    created = row is None
    if row is None:
        row = BidOpeningRoundModel(external_key=external_key, bid_notice_no="")
        db.add(row)

    status_label = (
        _clean(summary.get("progrsDivCdNm"))
        or _clean(summary.get("opengRsltDivNm"))
        or _clean(summary.get("opengRsltNtcCntnts"))
    )
    row.business_type = business_type
    row.bid_notice_no = _clean(summary.get("bidNtceNo"))
    row.bid_notice_ord = _clean(summary.get("bidNtceOrd")) or "00"
    row.bid_class_no = _clean(summary.get("bidClsfcNo")) or "0"
    row.rebid_no = _clean(summary.get("rbidNo")) or "0"
    row.title = _clean(summary.get("bidNtceNm")) or None
    row.status_label = status_label or None
    row.status = normalize_status(status_label).value
    row.opened_at = _parse_datetime(summary.get("opengDt") or summary.get("rlOpengDt"))
    row.participant_count = _parse_int(summary.get("prtcptCnum"))
    row.opening_notice = _clean(summary.get("opengRsltNtcCntnts")) or None
    row.notice_agency_code = _clean(summary.get("ntceInsttCd")) or None
    row.notice_agency_name = _clean(summary.get("ntceInsttNm")) or None
    row.demand_agency_code = _clean(summary.get("dminsttCd")) or None
    row.demand_agency_name = _clean(summary.get("dminsttNm")) or None
    row.collected_at = datetime.now(timezone.utc)

    if winner:
        _apply_winner(db, row, winner)
    elif row.winner_business_no or row.winner_company_name:
        row.status = OpeningStatus.AWARDED.value

    _store_snapshot(db, entity_type="ROUND", entity_key=external_key, payload=summary)
    if winner:
        _store_snapshot(db, entity_type="WINNER", entity_key=external_key, payload=winner)
    db.flush()
    return row, created


def _collect_round_entries(
    db: Session,
    client: OpeningResultApiClient,
    round_row: BidOpeningRoundModel,
    source: dict[str, Any],
) -> tuple[int, int, int]:
    entries = client.fetch_entries(source)
    if not entries:
        round_row.entries_collected_at = None
        db.flush()
        return 0, 0, 0
    inserted_count = 0
    updated_count = 0
    seen_external_keys: set[str] = set()
    for entry in entries:
        row, entry_created = _upsert_entry(db, round_row, entry)
        seen_external_keys.add(row.external_key)
        if entry_created:
            inserted_count += 1
        else:
            updated_count += 1
    stored_entries = db.execute(
        select(BidOpeningEntryModel).where(
            BidOpeningEntryModel.round_id == round_row.id
        )
    ).scalars()
    for stored_entry in stored_entries:
        if stored_entry.external_key not in seen_external_keys:
            db.delete(stored_entry)
            updated_count += 1
    round_row.entries_collected_at = datetime.now(timezone.utc)
    db.flush()
    return len(entries), inserted_count, updated_count


def _upsert_entry(
    db: Session,
    round_row: BidOpeningRoundModel,
    payload: dict[str, Any],
) -> tuple[BidOpeningEntryModel, bool]:
    external_key = _entry_external_key(payload, round_row.external_key)
    row = db.execute(
        select(BidOpeningEntryModel).where(BidOpeningEntryModel.external_key == external_key)
    ).scalar_one_or_none()
    created = row is None
    if row is None:
        row = BidOpeningEntryModel(
            round_id=round_row.id,
            external_key=external_key,
        )
        db.add(row)

    business_no = _clean(payload.get("prcbdrBizno")) or None
    company_name = _clean(payload.get("prcbdrNm")) or None
    row.round_id = round_row.id
    row.rank = _parse_int(payload.get("opengRank"))
    row.business_no = business_no
    row.company_name = company_name
    row.ceo_name = _clean(payload.get("prcbdrCeoNm")) or None
    row.bid_amount = _parse_decimal(payload.get("bidprcAmt"))
    row.bid_rate = _parse_decimal(payload.get("bidprcrt"))
    row.bid_price_score = _parse_decimal(payload.get("bidPrceEvlVal"))
    row.technical_raw_score = _parse_decimal(payload.get("techEvlNaturVal"))
    row.technical_score = _parse_decimal(payload.get("techEvlVal"))
    row.official_total_score = _parse_decimal(payload.get("totalEvlAmtVal"))
    row.total_score = (
        row.bid_price_score + row.technical_score
        if row.bid_price_score is not None and row.technical_score is not None
        else None
    )
    row.bid_at = _parse_datetime(payload.get("bidprcDt"))
    row.note = _clean(payload.get("rmrk")) or None
    row.draw_number_1 = _clean(payload.get("drwtNo1")) or None
    row.draw_number_2 = _clean(payload.get("drwtNo2")) or None
    row.is_winner = bool(
        (round_row.winner_business_no and business_no == round_row.winner_business_no)
        or (
            round_row.winner_company_name
            and company_name == round_row.winner_company_name
        )
    )

    _store_snapshot(db, entity_type="ENTRY", entity_key=external_key, payload=payload)
    db.flush()
    return row, created


def collect_opening_results(
    db: Session,
    request: CollectOpeningResultsRequest,
    client: OpeningResultApiClient | None = None,
    *,
    collection_run_id: int | None = None,
    collection_claim_token: str | None = None,
) -> CollectOpeningResultsResponse:
    uses_default_client = client is None
    client = client or OpeningResultApiClient(OpeningResultApiConfig.from_env())
    business_type = request.business_type.value
    global_claim_token = _claim_global_collection_lease(db, business_type)

    inserted_round_count = 0
    updated_round_count = 0
    inserted_entry_count = 0
    updated_entry_count = 0
    fetched_entry_count = 0
    skipped_count = 0
    processed_round_keys: set[str] = set()
    entry_candidates: dict[str, dict[str, Any]] = {}

    try:
        summaries = client.search_rounds(
            request.business_type,
            request.start_at,
            request.end_at,
        )
        winners = client.search_winners(
            request.business_type,
            request.start_at,
            request.end_at,
        )
        winners_by_key = _winner_index(winners, business_type)
        for summary in summaries:
            try:
                external_key = build_round_external_key(summary, business_type)
            except ValueError:
                skipped_count += 1
                continue
            round_row, created = _upsert_round(
                db,
                summary,
                winners_by_key.get(external_key),
                business_type,
            )
            processed_round_keys.add(external_key)
            if created:
                inserted_round_count += 1
            else:
                updated_round_count += 1

            if request.include_entries:
                entry_candidates[external_key] = summary

        for winner in winners:
            try:
                external_key = build_round_external_key(winner, business_type)
            except ValueError:
                skipped_count += 1
                continue
            if external_key in processed_round_keys:
                continue

            round_row = db.execute(
                select(BidOpeningRoundModel).where(
                    BidOpeningRoundModel.external_key == external_key
                )
            ).scalar_one_or_none()
            if round_row is None:
                round_row, _ = _upsert_round(
                    db,
                    winner,
                    winner,
                    business_type,
                )
                inserted_round_count += 1
            else:
                _apply_winner(db, round_row, winner)
                round_row.collected_at = datetime.now(timezone.utc)
                _store_snapshot(
                    db,
                    entity_type="WINNER",
                    entity_key=external_key,
                    payload=winner,
                )
                db.flush()
                updated_round_count += 1
            processed_round_keys.add(external_key)

            if request.include_entries:
                entry_candidates[external_key] = winner
        _refresh_collection_lease(
            db,
            collection_run_id=collection_run_id,
            collection_claim_token=collection_claim_token,
            business_type=business_type,
            global_claim_token=global_claim_token,
        )
        db.commit()
        sync_organization_matches(db)
        sync_user_matches(db)
        _refresh_collection_lease(
            db,
            collection_run_id=collection_run_id,
            collection_claim_token=collection_claim_token,
            business_type=business_type,
            global_claim_token=global_claim_token,
        )
        db.commit()
        source_rounds = db.scalars(
            select(BidOpeningRoundModel).where(
                BidOpeningRoundModel.business_type == business_type
            )
        ).all()
        if uses_default_client:
            enrich_bid_notice_contexts_for_opening_rounds(db, source_rounds)
            _refresh_collection_lease(
                db,
                collection_run_id=collection_run_id,
                collection_claim_token=collection_claim_token,
                business_type=business_type,
                global_claim_token=global_claim_token,
            )
            db.commit()
        pending_detail_keys = {
            round_row.external_key
            for round_row in source_rounds
            if round_row.status
            in {OpeningStatus.OPENED.value, OpeningStatus.AWARDED.value}
            and round_row.entries_collected_at is None
        }
        if pending_detail_keys:
            snapshots = db.scalars(
                select(BidResultSnapshotModel)
                .where(
                    BidResultSnapshotModel.entity_type == "ROUND",
                    BidResultSnapshotModel.entity_key.in_(pending_detail_keys),
                )
                .order_by(BidResultSnapshotModel.id.desc())
            )
            for snapshot in snapshots:
                if snapshot.entity_key in entry_candidates:
                    continue
                try:
                    entry_candidates[snapshot.entity_key] = json.loads(
                        snapshot.raw_payload
                    )
                except (TypeError, ValueError):
                    continue
        if entry_candidates:
            for external_key, source in entry_candidates.items():
                round_row = db.scalar(
                    select(BidOpeningRoundModel).where(
                        BidOpeningRoundModel.external_key == external_key
                    )
                )
                if round_row is None or round_row.status not in {
                    OpeningStatus.OPENED.value,
                    OpeningStatus.AWARDED.value,
                }:
                    continue
                fetched, inserted, updated = _collect_round_entries(
                    db,
                    client,
                    round_row,
                    source,
                )
                fetched_entry_count += fetched
                inserted_entry_count += inserted
                updated_entry_count += updated
            _refresh_collection_lease(
                db,
                collection_run_id=collection_run_id,
                collection_claim_token=collection_claim_token,
                business_type=business_type,
                global_claim_token=global_claim_token,
            )
            db.commit()
    except Exception:
        db.rollback()
        _release_global_collection_lease(
            db,
            business_type=business_type,
            global_claim_token=global_claim_token,
        )
        raise

    _release_global_collection_lease(
        db,
        business_type=business_type,
        global_claim_token=global_claim_token,
    )

    return CollectOpeningResultsResponse(
        fetched_round_count=len(processed_round_keys),
        fetched_entry_count=fetched_entry_count,
        inserted_round_count=inserted_round_count,
        updated_round_count=updated_round_count,
        inserted_entry_count=inserted_entry_count,
        updated_entry_count=updated_entry_count,
        skipped_count=skipped_count,
    )


def build_scheduled_collection_window(
    now: datetime | None = None,
) -> tuple[str, datetime, datetime]:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=KST)
    local_current = current.astimezone(KST)
    slot_hour = next(
        (
            hour
            for hour in reversed(COLLECTION_SLOT_HOURS)
            if local_current.hour >= hour
        ),
        None,
    )
    if slot_hour is None:
        local_end = (local_current - timedelta(days=1)).replace(
            hour=COLLECTION_SLOT_HOURS[-1],
            minute=0,
            second=0,
            microsecond=0,
        )
    else:
        local_end = local_current.replace(
            hour=slot_hour,
            minute=0,
            second=0,
            microsecond=0,
        )

    slot_index = COLLECTION_SLOT_HOURS.index(local_end.hour)
    if slot_index == 0:
        local_start = (local_end - timedelta(days=1)).replace(
            hour=COLLECTION_SLOT_HOURS[-1]
        )
    else:
        local_start = local_end.replace(
            hour=COLLECTION_SLOT_HOURS[slot_index - 1]
        )
    window_end = local_end.astimezone(timezone.utc)
    window_start = local_start.astimezone(timezone.utc)
    run_key = f"SERVICE:{local_end.strftime('%Y%m%d%H')}"
    return run_key, window_start, window_end


def _scheduled_response(
    run: BidOpeningCollectionRunModel,
    *,
    skipped_existing_run: bool,
) -> ScheduledCollectOpeningResultsResponse:
    return ScheduledCollectOpeningResultsResponse(
        run_key=run.run_key,
        window_start=run.window_start,
        window_end=run.window_end,
        run_status=run.status,
        skipped_existing_run=skipped_existing_run,
        fetched_round_count=run.fetched_round_count,
        fetched_entry_count=run.fetched_entry_count,
        inserted_round_count=run.inserted_round_count,
        updated_round_count=run.updated_round_count,
        inserted_entry_count=run.inserted_entry_count,
        updated_entry_count=run.updated_entry_count,
        skipped_count=run.skipped_count,
    )


def run_scheduled_opening_results(
    db: Session,
    *,
    now: datetime | None = None,
    client: OpeningResultApiClient | None = None,
) -> ScheduledCollectOpeningResultsResponse:
    run_key, window_start, window_end = build_scheduled_collection_window(now)
    claimed_at = datetime.now(timezone.utc)
    lease_cutoff = claimed_at - timedelta(minutes=COLLECTION_LEASE_MINUTES)
    claim_token = str(uuid4())
    run = db.execute(
        select(BidOpeningCollectionRunModel).where(
            BidOpeningCollectionRunModel.run_key == run_key
        )
    ).scalar_one_or_none()
    if run is None:
        last_successful_end = db.scalar(
            select(func.max(BidOpeningCollectionRunModel.window_end)).where(
                BidOpeningCollectionRunModel.status == "SUCCESS",
                BidOpeningCollectionRunModel.window_end <= window_end,
            )
        )
        if last_successful_end is not None:
            if last_successful_end.tzinfo is None:
                last_successful_end = last_successful_end.replace(tzinfo=timezone.utc)
            if last_successful_end < window_start:
                window_start = max(
                    last_successful_end,
                    window_end - timedelta(days=FRONT_LIST_DAYS),
                )
    else:
        window_start = run.window_start
        window_end = run.window_end
    if run is not None and run.status == "SUCCESS":
        return _scheduled_response(run, skipped_existing_run=True)
    if run is not None and run.status == "RUNNING":
        started_at = run.started_at
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=timezone.utc)
        if started_at > lease_cutoff:
            return _scheduled_response(run, skipped_existing_run=True)

    if run is None:
        run = BidOpeningCollectionRunModel(
            run_key=run_key,
            business_type="SERVICE",
            window_start=window_start,
            window_end=window_end,
            status="RUNNING",
            claim_token=claim_token,
            started_at=claimed_at,
        )
        db.add(run)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            run = db.execute(
                select(BidOpeningCollectionRunModel).where(
                    BidOpeningCollectionRunModel.run_key == run_key
                )
            ).scalar_one()
            return _scheduled_response(run, skipped_existing_run=True)
    else:
        claim_result = db.execute(
            update(BidOpeningCollectionRunModel)
            .where(BidOpeningCollectionRunModel.id == run.id)
            .where(
                or_(
                    BidOpeningCollectionRunModel.status != "RUNNING",
                    BidOpeningCollectionRunModel.started_at <= lease_cutoff,
                )
            )
            .values(
                status="RUNNING",
                claim_token=claim_token,
                error_message=None,
                started_at=claimed_at,
                finished_at=None,
                fetched_round_count=0,
                fetched_entry_count=0,
                inserted_round_count=0,
                updated_round_count=0,
                inserted_entry_count=0,
                updated_entry_count=0,
                skipped_count=0,
            )
            .execution_options(synchronize_session=False)
        )
        if claim_result.rowcount != 1:
            db.rollback()
            run = db.execute(
                select(BidOpeningCollectionRunModel).where(
                    BidOpeningCollectionRunModel.run_key == run_key
                )
            ).scalar_one()
            return _scheduled_response(run, skipped_existing_run=True)
        db.commit()
        run = db.get(BidOpeningCollectionRunModel, run.id)
    db.refresh(run)

    request = CollectOpeningResultsRequest(
        start_at=window_start,
        end_at=window_end,
    )
    try:
        result = collect_opening_results(
            db,
            request,
            client,
            collection_run_id=run.id,
            collection_claim_token=claim_token,
        )
    except Exception as error:
        db.execute(
            update(BidOpeningCollectionRunModel)
            .where(
                BidOpeningCollectionRunModel.id == run.id,
                BidOpeningCollectionRunModel.claim_token == claim_token,
            )
            .values(
                status="FAILED",
                claim_token=None,
                error_message=str(error)[:2000],
                finished_at=datetime.now(timezone.utc),
            )
            .execution_options(synchronize_session=False)
        )
        db.commit()
        raise

    completion = db.execute(
        update(BidOpeningCollectionRunModel)
        .where(
            BidOpeningCollectionRunModel.id == run.id,
            BidOpeningCollectionRunModel.claim_token == claim_token,
        )
        .values(
            status="SUCCESS",
            claim_token=None,
            fetched_round_count=result.fetched_round_count,
            fetched_entry_count=result.fetched_entry_count,
            inserted_round_count=result.inserted_round_count,
            updated_round_count=result.updated_round_count,
            inserted_entry_count=result.inserted_entry_count,
            updated_entry_count=result.updated_entry_count,
            skipped_count=result.skipped_count,
            finished_at=datetime.now(timezone.utc),
        )
        .execution_options(synchronize_session=False)
    )
    if completion.rowcount != 1:
        db.rollback()
        db.expire_all()
        current_run = db.get(BidOpeningCollectionRunModel, run.id)
        return _scheduled_response(current_run, skipped_existing_run=True)
    db.commit()
    db.expire_all()
    run = db.get(BidOpeningCollectionRunModel, run.id)
    db.refresh(run)
    return _scheduled_response(run, skipped_existing_run=False)


def list_opening_results(
    db: Session,
    query: OpeningResultListQuery,
    *,
    organization_id: int | None = None,
    user_id: int | None = None,
    paginate: bool = True,
) -> tuple[list[BidOpeningRoundModel], int]:
    organization_scoped = organization_id is not None and user_id is not None
    if organization_scoped:
        statement = select(BidOpeningRoundModel, UserOpeningResultMatchModel).join(
            UserOpeningResultMatchModel,
            UserOpeningResultMatchModel.round_id == BidOpeningRoundModel.id,
        )
        count_statement = select(func.count(BidOpeningRoundModel.id)).join(
            UserOpeningResultMatchModel,
            UserOpeningResultMatchModel.round_id == BidOpeningRoundModel.id,
        )
    else:
        statement = select(BidOpeningRoundModel)
        count_statement = select(func.count(BidOpeningRoundModel.id))
    filters = []
    if organization_scoped:
        filters.extend(visible_result_predicates(organization_id, user_id))
    if query.q:
        keyword = f"%{query.q.strip()}%"
        official_notice_matches = exists(
            select(ScraperNoticeModel.id).where(
                ScraperNoticeModel.bid_notice_no == BidOpeningRoundModel.bid_notice_no,
                or_(
                    ScraperNoticeModel.business_name.like(keyword),
                    ScraperNoticeModel.demand_agency_name.like(keyword),
                ),
            )
        )
        filters.append(
            or_(
                BidOpeningRoundModel.bid_notice_no.like(keyword),
                BidOpeningRoundModel.title.like(keyword),
                BidOpeningRoundModel.notice_agency_name.like(keyword),
                BidOpeningRoundModel.demand_agency_name.like(keyword),
                BidOpeningRoundModel.winner_company_name.like(keyword),
                official_notice_matches,
            )
        )
    if query.status:
        filters.append(BidOpeningRoundModel.status == query.status.value)
    now = datetime.now(timezone.utc)
    opened_from = query.opened_from or now - timedelta(days=FRONT_LIST_DAYS)
    opened_to = query.opened_to or now
    filters.append(BidOpeningRoundModel.opened_at >= opened_from)
    filters.append(BidOpeningRoundModel.opened_at <= opened_to)
    if filters:
        statement = statement.where(*filters)
        count_statement = count_statement.where(*filters)

    total = int(db.execute(count_statement).scalar_one())
    statement = statement.order_by(
        BidOpeningRoundModel.opened_at.desc(), BidOpeningRoundModel.id.desc()
    )
    if paginate:
        statement = statement.offset((query.page - 1) * query.page_size).limit(
            query.page_size
        )
    result = db.execute(statement)
    if organization_scoped:
        rows = []
        for round_row, match in result.all():
            round_row.matched_keywords = normalize_keywords(match.matched_keywords)
            rows.append(round_row)
    else:
        rows = list(result.scalars().all())
    return rows, total


def get_opening_result(
    db: Session,
    result_id: int,
    *,
    organization_id: int | None = None,
    user_id: int | None = None,
) -> tuple[BidOpeningRoundModel, list[BidOpeningEntryModel]] | None:
    if organization_id is not None and user_id is not None:
        visible = get_visible_result_match(
            db,
            organization_id=organization_id,
            user_id=user_id,
            result_id=result_id,
        )
        if visible is None:
            return None
        round_row, match = visible
        round_row.matched_keywords = normalize_keywords(match.matched_keywords)
    else:
        round_row = db.get(BidOpeningRoundModel, result_id)
        if round_row is None:
            return None
    entries = (
        db.execute(
            select(BidOpeningEntryModel)
            .where(BidOpeningEntryModel.round_id == result_id)
            .order_by(BidOpeningEntryModel.rank.asc(), BidOpeningEntryModel.id.asc())
        )
        .scalars()
        .all()
    )
    return round_row, list(entries)
