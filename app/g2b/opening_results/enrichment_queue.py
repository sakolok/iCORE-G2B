from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import and_, exists, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.data.models import ScraperNoticeModel
from app.g2b.bid_notice import (
    REGION_API_EMPTY,
    REGION_API_ERROR,
    REGION_API_ORDER_MISMATCH,
    REGION_API_VALUE,
    canonical_bid_notice_identity,
    missing_bid_notice_context_fields,
)
from app.g2b.opening_results.models import (
    BidNoticeEnrichmentJobModel,
    BidOpeningRoundModel,
    UserOpeningResultMatchModel,
    UserOpeningResultStateModel,
)


ENRICHMENT_TASK_NOTICE_CONTEXT = "NOTICE_CONTEXT"
ENRICHMENT_STATUS_PENDING = "PENDING"
ENRICHMENT_STATUS_RUNNING = "RUNNING"
ENRICHMENT_STATUS_RETRY_WAIT = "RETRY_WAIT"
ENRICHMENT_STATUS_SUCCEEDED = "SUCCEEDED"
ENRICHMENT_STATUS_NEEDS_REVIEW = "NEEDS_REVIEW"
ENRICHMENT_PRIORITY_BUSINESS_AMOUNT = 100
ENRICHMENT_PRIORITY_NOTICE_CONTEXT = 50
ACTIVE_RESULT_DAYS = 14
USER_TERMINAL_STATES = ("EXPORTED", "DISMISSED")
ENRICHMENT_LEASE_MINUTES = 15
API_ERROR_RETRY_DELAYS = (
    timedelta(hours=1),
    timedelta(hours=6),
    timedelta(hours=24),
)


@dataclass(frozen=True)
class NoticeEnrichmentRunResult:
    claimed_count: int
    succeeded_count: int
    needs_review_count: int
    retry_scheduled_count: int


def enqueue_notice_enrichment_jobs(
    db: Session,
    *,
    now: datetime | None = None,
) -> int:
    current = now or datetime.now(timezone.utc)
    cutoff = current - timedelta(days=ACTIVE_RESULT_DAYS)
    handled_by_user = exists(
        select(UserOpeningResultStateModel.id).where(
            UserOpeningResultStateModel.user_id
            == UserOpeningResultMatchModel.user_id,
            UserOpeningResultStateModel.result_external_key
            == UserOpeningResultMatchModel.result_external_key,
            UserOpeningResultStateModel.state.in_(USER_TERMINAL_STATES),
        )
    ).correlate(UserOpeningResultMatchModel)
    rounds = (
        db.execute(
            select(BidOpeningRoundModel)
            .join(
                UserOpeningResultMatchModel,
                UserOpeningResultMatchModel.round_id == BidOpeningRoundModel.id,
            )
            .where(
                UserOpeningResultMatchModel.is_current_match.is_(True),
                ~handled_by_user,
                or_(
                    BidOpeningRoundModel.opened_at >= cutoff,
                    BidOpeningRoundModel.collected_at >= cutoff,
                ),
            )
            .distinct()
        )
        .scalars()
        .all()
    )
    identities = {
        identity
        for round_row in rounds
        if (
            identity := canonical_bid_notice_identity(
                round_row.bid_notice_no,
                round_row.bid_notice_ord,
            )
        )
        is not None
    }
    if not identities:
        return 0

    notice_numbers = {identity[0] for identity in identities}
    context_rows = (
        db.execute(
            select(ScraperNoticeModel).where(
                ScraperNoticeModel.bid_notice_no.in_(notice_numbers)
            )
        )
        .scalars()
        .all()
    )
    contexts_by_identity: dict[tuple[str, str], list[ScraperNoticeModel]] = {}
    for context in context_rows:
        identity = canonical_bid_notice_identity(
            context.bid_notice_no,
            context.bid_notice_ord,
        )
        if identity in identities:
            contexts_by_identity.setdefault(identity, []).append(context)

    priorities: dict[tuple[str, str], int] = {}
    for identity in identities:
        contexts = contexts_by_identity.get(identity, [])
        if len(contexts) != 1 or contexts[0].base_amount is None:
            priorities[identity] = ENRICHMENT_PRIORITY_BUSINESS_AMOUNT
            continue
        context = contexts[0]
        missing_fields = set(missing_bid_notice_context_fields(context))
        if not missing_fields:
            continue
        if (
            context.region_restriction_api_status
            in {REGION_API_EMPTY, REGION_API_ORDER_MISMATCH}
            and missing_fields == {"region_restriction"}
        ):
            continue
        priorities[identity] = ENRICHMENT_PRIORITY_NOTICE_CONTEXT

    if not priorities:
        return 0

    existing_jobs = (
        db.execute(
            select(BidNoticeEnrichmentJobModel).where(
                BidNoticeEnrichmentJobModel.bid_notice_no.in_(
                    {identity[0] for identity in priorities}
                ),
                BidNoticeEnrichmentJobModel.task_type
                == ENRICHMENT_TASK_NOTICE_CONTEXT,
            )
        )
        .scalars()
        .all()
    )
    existing_by_identity = {
        (job.bid_notice_no, job.bid_notice_ord): job for job in existing_jobs
    }

    created_count = 0
    for notice_no, notice_ord in sorted(priorities):
        priority = priorities[(notice_no, notice_ord)]
        existing = existing_by_identity.get((notice_no, notice_ord))
        if existing is not None:
            if existing.status in {"PENDING", "RETRY_WAIT"}:
                existing.priority = max(
                    existing.priority,
                    priority,
                )
            continue
        try:
            with db.begin_nested():
                db.add(
                    BidNoticeEnrichmentJobModel(
                        bid_notice_no=notice_no,
                        bid_notice_ord=notice_ord,
                        task_type=ENRICHMENT_TASK_NOTICE_CONTEXT,
                        status=ENRICHMENT_STATUS_PENDING,
                        priority=priority,
                    )
                )
                db.flush()
        except IntegrityError:
            continue
        created_count += 1
    return created_count


def _claim_ready_jobs(
    db: Session,
    *,
    limit: int,
    current: datetime,
) -> tuple[str, list[int]]:
    claim_token = str(uuid4())
    stale_before = current - timedelta(minutes=ENRICHMENT_LEASE_MINUTES)
    ready = or_(
        BidNoticeEnrichmentJobModel.status == ENRICHMENT_STATUS_PENDING,
        and_(
            BidNoticeEnrichmentJobModel.status == ENRICHMENT_STATUS_RETRY_WAIT,
            or_(
                BidNoticeEnrichmentJobModel.next_retry_at.is_(None),
                BidNoticeEnrichmentJobModel.next_retry_at <= current,
            ),
        ),
        and_(
            BidNoticeEnrichmentJobModel.status == ENRICHMENT_STATUS_RUNNING,
            BidNoticeEnrichmentJobModel.claimed_at <= stale_before,
        ),
    )
    jobs = (
        db.execute(
            select(BidNoticeEnrichmentJobModel)
            .where(ready)
            .order_by(
                BidNoticeEnrichmentJobModel.priority.desc(),
                BidNoticeEnrichmentJobModel.created_at,
                BidNoticeEnrichmentJobModel.id,
            )
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        .scalars()
        .all()
    )
    for job in jobs:
        job.status = ENRICHMENT_STATUS_RUNNING
        job.claim_token = claim_token
        job.claimed_at = current
    db.commit()
    return claim_token, [job.id for job in jobs]


def _find_round_for_job(
    db: Session,
    job: BidNoticeEnrichmentJobModel,
) -> BidOpeningRoundModel | None:
    rounds = (
        db.execute(
            select(BidOpeningRoundModel)
            .where(BidOpeningRoundModel.bid_notice_no == job.bid_notice_no)
            .order_by(BidOpeningRoundModel.opened_at.desc(), BidOpeningRoundModel.id)
        )
        .scalars()
        .all()
    )
    expected = (job.bid_notice_no, job.bid_notice_ord)
    return next(
        (
            round_row
            for round_row in rounds
            if canonical_bid_notice_identity(
                round_row.bid_notice_no,
                round_row.bid_notice_ord,
            )
            == expected
        ),
        None,
    )


def _load_contexts_for_job(
    db: Session,
    job: BidNoticeEnrichmentJobModel,
) -> list[ScraperNoticeModel]:
    contexts = (
        db.execute(
            select(ScraperNoticeModel).where(
                ScraperNoticeModel.bid_notice_no == job.bid_notice_no
            )
        )
        .scalars()
        .all()
    )
    expected = (job.bid_notice_no, job.bid_notice_ord)
    return [
        context
        for context in contexts
        if canonical_bid_notice_identity(
            context.bid_notice_no,
            context.bid_notice_ord,
        )
        == expected
    ]


def _complete_job(
    job: BidNoticeEnrichmentJobModel,
    *,
    status: str,
    current: datetime,
    error: str | None = None,
) -> None:
    job.status = status
    job.claim_token = None
    job.claimed_at = None
    job.next_retry_at = None
    job.last_error = error
    job.completed_at = current


def _schedule_api_error_retry(
    job: BidNoticeEnrichmentJobModel,
    *,
    current: datetime,
    error: str,
) -> bool:
    job.retry_count += 1
    job.claim_token = None
    job.claimed_at = None
    job.last_error = error[:2000]
    job.completed_at = None
    if job.retry_count <= len(API_ERROR_RETRY_DELAYS):
        job.status = ENRICHMENT_STATUS_RETRY_WAIT
        job.next_retry_at = current + API_ERROR_RETRY_DELAYS[job.retry_count - 1]
        return True
    job.status = ENRICHMENT_STATUS_NEEDS_REVIEW
    job.next_retry_at = None
    job.completed_at = current
    return False


def _default_enrich_notice_context(
    db: Session,
    round_row: BidOpeningRoundModel,
) -> int:
    from app.g2b.bid_notices.service import (
        enrich_bid_notice_contexts_for_opening_rounds,
    )

    return enrich_bid_notice_contexts_for_opening_rounds(db, [round_row])


def process_notice_enrichment_jobs(
    db: Session,
    *,
    limit: int = 20,
    now: datetime | None = None,
    enrich_notice_context: (
        Callable[[Session, BidOpeningRoundModel], int] | None
    ) = None,
) -> NoticeEnrichmentRunResult:
    if limit < 1 or limit > 100:
        raise ValueError("limit은 1 이상 100 이하여야 합니다.")
    current = now or datetime.now(timezone.utc)
    claim_token, job_ids = _claim_ready_jobs(db, limit=limit, current=current)
    enrich_context = enrich_notice_context or _default_enrich_notice_context
    succeeded_count = 0
    needs_review_count = 0
    retry_scheduled_count = 0

    for job_id in job_ids:
        job = db.get(BidNoticeEnrichmentJobModel, job_id)
        if job is None or job.claim_token != claim_token:
            continue
        try:
            round_row = _find_round_for_job(db, job)
            if round_row is None:
                _complete_job(
                    job,
                    status=ENRICHMENT_STATUS_NEEDS_REVIEW,
                    current=current,
                    error="OPENING_ROUND_MISSING",
                )
                needs_review_count += 1
                db.commit()
                continue

            enriched_count = enrich_context(db, round_row)
            contexts = _load_contexts_for_job(db, job)
            if len(contexts) > 1:
                _complete_job(
                    job,
                    status=ENRICHMENT_STATUS_NEEDS_REVIEW,
                    current=current,
                    error="NOTICE_CONTEXT_AMBIGUOUS",
                )
                needs_review_count += 1
                db.commit()
                continue
            if not contexts:
                retrying = _schedule_api_error_retry(
                    job,
                    current=current,
                    error=(
                        "NOTICE_CONTEXT_API_ERROR"
                        if enriched_count == 0
                        else "NOTICE_CONTEXT_MISSING"
                    ),
                )
                retry_scheduled_count += int(retrying)
                needs_review_count += int(not retrying)
                db.commit()
                continue

            context = contexts[0]
            missing_fields = set(missing_bid_notice_context_fields(context))
            if context.base_amount is None and enriched_count == 0:
                retrying = _schedule_api_error_retry(
                    job,
                    current=current,
                    error="NOTICE_CONTEXT_API_ERROR",
                )
                retry_scheduled_count += int(retrying)
                needs_review_count += int(not retrying)
            elif context.region_restriction_api_status == REGION_API_ERROR:
                retrying = _schedule_api_error_retry(
                    job,
                    current=current,
                    error="REGION_API_ERROR",
                )
                retry_scheduled_count += int(retrying)
                needs_review_count += int(not retrying)
            elif context.base_amount is None:
                _complete_job(
                    job,
                    status=ENRICHMENT_STATUS_NEEDS_REVIEW,
                    current=current,
                    error="BUSINESS_AMOUNT_EMPTY",
                )
                needs_review_count += 1
            elif context.region_restriction_api_status == REGION_API_EMPTY:
                _complete_job(
                    job,
                    status=ENRICHMENT_STATUS_NEEDS_REVIEW,
                    current=current,
                    error="REGION_API_EMPTY",
                )
                needs_review_count += 1
            elif (
                context.region_restriction_api_status
                == REGION_API_ORDER_MISMATCH
            ):
                _complete_job(
                    job,
                    status=ENRICHMENT_STATUS_NEEDS_REVIEW,
                    current=current,
                    error="REGION_API_ORDER_MISMATCH",
                )
                needs_review_count += 1
            elif missing_fields:
                _complete_job(
                    job,
                    status=ENRICHMENT_STATUS_NEEDS_REVIEW,
                    current=current,
                    error=(
                        "NOTICE_CONTEXT_INCOMPLETE:"
                        + ",".join(sorted(missing_fields))
                    ),
                )
                needs_review_count += 1
            elif (
                context.region_restriction_api_status == REGION_API_VALUE
                or context.region_restriction
            ):
                _complete_job(
                    job,
                    status=ENRICHMENT_STATUS_SUCCEEDED,
                    current=current,
                )
                succeeded_count += 1
            else:
                _complete_job(
                    job,
                    status=ENRICHMENT_STATUS_NEEDS_REVIEW,
                    current=current,
                    error="REGION_STATUS_UNKNOWN",
                )
                needs_review_count += 1
            db.commit()
        except Exception as error:
            db.rollback()
            job = db.get(BidNoticeEnrichmentJobModel, job_id)
            if job is None or job.claim_token != claim_token:
                continue
            retrying = _schedule_api_error_retry(
                job,
                current=current,
                error=f"{type(error).__name__}: {error}",
            )
            retry_scheduled_count += int(retrying)
            needs_review_count += int(not retrying)
            db.commit()

    return NoticeEnrichmentRunResult(
        claimed_count=len(job_ids),
        succeeded_count=succeeded_count,
        needs_review_count=needs_review_count,
        retry_scheduled_count=retry_scheduled_count,
    )
