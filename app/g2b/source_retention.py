from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.data.models import ScraperNoticeModel
from app.g2b.bid_notices.models import (
    BidNoticeDocumentAnalysisModel,
    BidNoticeSheetExportModel,
    UserBidNoticeMatchModel,
    UserBidNoticeStateModel,
)
from app.g2b.opening_results.models import (
    BidNoticeEnrichmentJobModel,
    BidOpeningEntryModel,
    BidOpeningRoundModel,
    BidResultSnapshotModel,
    OrganizationOpeningResultMatchModel,
    SheetExportModel,
    UserOpeningResultMatchModel,
    UserOpeningResultStateModel,
)
from app.g2b.pre_specifications.models import (
    PreSpecificationModel,
    PreSpecificationSheetExportModel,
    PreSpecificationSnapshotModel,
    UserPreSpecificationStateModel,
)


SOURCE_RETENTION_DAYS = 30


@dataclass(frozen=True)
class SourceRetentionPurgeResult:
    pre_specification_count: int = 0
    bid_notice_count: int = 0
    opening_result_count: int = 0
    snapshot_count: int = 0
    enrichment_job_count: int = 0


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def purge_expired_source_data(
    db: Session,
    *,
    now: datetime | None = None,
) -> SourceRetentionPurgeResult:
    """Delete raw G2B source records retained for more than 30 days.

    The linked per-user archive and Sheet-export rows are removed with their
    source record.  The Sheet row itself has already been written outside the
    database, while collection-run logs, user settings, and Sheet destinations
    remain available for operations.
    """

    current = now or _utcnow()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    cutoff = current.astimezone(timezone.utc) - timedelta(days=SOURCE_RETENTION_DAYS)

    pre_specification_ids = db.scalars(
        select(PreSpecificationModel.bf_spec_rgst_no).where(
            PreSpecificationModel.last_seen_at < cutoff
        )
    ).all()
    if pre_specification_ids:
        db.execute(
            delete(PreSpecificationSnapshotModel).where(
                PreSpecificationSnapshotModel.bf_spec_rgst_no.in_(pre_specification_ids)
            )
        )
        db.execute(
            delete(UserPreSpecificationStateModel).where(
                UserPreSpecificationStateModel.bf_spec_rgst_no.in_(pre_specification_ids)
            )
        )
        db.execute(
            delete(PreSpecificationSheetExportModel).where(
                PreSpecificationSheetExportModel.bf_spec_rgst_no.in_(pre_specification_ids)
            )
        )
        db.execute(
            delete(PreSpecificationModel).where(
                PreSpecificationModel.bf_spec_rgst_no.in_(pre_specification_ids)
            )
        )

    bid_notice_ids = db.scalars(
        select(ScraperNoticeModel.id).where(ScraperNoticeModel.last_seen_at < cutoff)
    ).all()
    if bid_notice_ids:
        db.execute(
            delete(BidNoticeDocumentAnalysisModel).where(
                BidNoticeDocumentAnalysisModel.notice_id.in_(bid_notice_ids)
            )
        )
        db.execute(
            delete(UserBidNoticeMatchModel).where(
                UserBidNoticeMatchModel.notice_id.in_(bid_notice_ids)
            )
        )
        db.execute(
            delete(UserBidNoticeStateModel).where(
                UserBidNoticeStateModel.notice_id.in_(bid_notice_ids)
            )
        )
        db.execute(
            delete(BidNoticeSheetExportModel).where(
                BidNoticeSheetExportModel.notice_id.in_(bid_notice_ids)
            )
        )
        db.execute(delete(ScraperNoticeModel).where(ScraperNoticeModel.id.in_(bid_notice_ids)))

    opening_result_rows = db.execute(
        select(BidOpeningRoundModel.id, BidOpeningRoundModel.external_key).where(
            BidOpeningRoundModel.collected_at < cutoff
        )
    ).all()
    opening_result_ids = [row.id for row in opening_result_rows]
    opening_result_keys = [row.external_key for row in opening_result_rows]
    if opening_result_ids:
        db.execute(
            delete(BidOpeningEntryModel).where(BidOpeningEntryModel.round_id.in_(opening_result_ids))
        )
        db.execute(
            delete(OrganizationOpeningResultMatchModel).where(
                OrganizationOpeningResultMatchModel.round_id.in_(opening_result_ids)
            )
        )
        db.execute(
            delete(UserOpeningResultMatchModel).where(
                UserOpeningResultMatchModel.round_id.in_(opening_result_ids)
            )
        )
        db.execute(
            delete(UserOpeningResultStateModel).where(
                UserOpeningResultStateModel.result_external_key.in_(opening_result_keys)
            )
        )
        db.execute(
            delete(SheetExportModel).where(
                SheetExportModel.result_external_key.in_(opening_result_keys)
            )
        )
        db.execute(delete(BidOpeningRoundModel).where(BidOpeningRoundModel.id.in_(opening_result_ids)))

    snapshot_count = db.execute(
        delete(BidResultSnapshotModel).where(BidResultSnapshotModel.collected_at < cutoff)
    ).rowcount or 0
    enrichment_job_count = db.execute(
        delete(BidNoticeEnrichmentJobModel).where(
            BidNoticeEnrichmentJobModel.updated_at < cutoff
        )
    ).rowcount or 0
    db.commit()

    return SourceRetentionPurgeResult(
        pre_specification_count=len(pre_specification_ids),
        bid_notice_count=len(bid_notice_ids),
        opening_result_count=len(opening_result_ids),
        snapshot_count=snapshot_count,
        enrichment_job_count=enrichment_job_count,
    )
