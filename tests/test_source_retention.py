import unittest
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.data.models import Base, ScraperNoticeModel
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
from app.g2b.source_retention import (
    SOURCE_RETENTION_DAYS,
    purge_expired_source_data,
)


class SourceRetentionTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.now = datetime(2026, 7, 29, 0, 0, tzinfo=timezone.utc)
        self.expired_at = self.now - timedelta(days=SOURCE_RETENTION_DAYS + 1)
        self.current_at = self.now - timedelta(days=SOURCE_RETENTION_DAYS - 1)

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_purge_deletes_expired_source_records_and_related_history(self):
        self._add_pre_specification("OLD-PRE", self.expired_at)
        self._add_pre_specification("CURRENT-PRE", self.current_at)
        old_notice_id = self._add_notice("old-notice", self.expired_at)
        current_notice_id = self._add_notice("current-notice", self.current_at)
        old_round_id = self._add_opening_result("old-round", self.expired_at)
        current_round_id = self._add_opening_result("current-round", self.current_at)
        self._add_related_rows(old_notice_id, "OLD-PRE", old_round_id, "old-round", self.expired_at)
        self._add_related_rows(
            current_notice_id,
            "CURRENT-PRE",
            current_round_id,
            "current-round",
            self.current_at,
        )
        self.db.commit()

        result = purge_expired_source_data(self.db, now=self.now)

        self.assertEqual(result.pre_specification_count, 1)
        self.assertEqual(result.bid_notice_count, 1)
        self.assertEqual(result.opening_result_count, 1)
        self.assertEqual(result.snapshot_count, 1)
        self.assertEqual(result.enrichment_job_count, 1)
        self.assertEqual(
            self.db.scalars(select(PreSpecificationModel.bf_spec_rgst_no)).all(),
            ["CURRENT-PRE"],
        )
        self.assertEqual(
            self.db.scalars(select(ScraperNoticeModel.notice_id)).all(),
            ["current-notice"],
        )
        self.assertEqual(
            self.db.scalars(select(BidOpeningRoundModel.external_key)).all(),
            ["current-round"],
        )
        self.assertEqual(len(self.db.scalars(select(PreSpecificationSnapshotModel)).all()), 1)
        self.assertEqual(len(self.db.scalars(select(PreSpecificationSheetExportModel)).all()), 1)
        self.assertEqual(len(self.db.scalars(select(UserPreSpecificationStateModel)).all()), 1)
        self.assertEqual(len(self.db.scalars(select(BidNoticeDocumentAnalysisModel)).all()), 1)
        self.assertEqual(len(self.db.scalars(select(BidNoticeSheetExportModel)).all()), 1)
        self.assertEqual(len(self.db.scalars(select(UserBidNoticeMatchModel)).all()), 1)
        self.assertEqual(len(self.db.scalars(select(UserBidNoticeStateModel)).all()), 1)
        self.assertEqual(len(self.db.scalars(select(BidOpeningEntryModel)).all()), 1)
        self.assertEqual(
            len(self.db.scalars(select(OrganizationOpeningResultMatchModel)).all()),
            1,
        )
        self.assertEqual(len(self.db.scalars(select(UserOpeningResultMatchModel)).all()), 1)
        self.assertEqual(len(self.db.scalars(select(UserOpeningResultStateModel)).all()), 1)
        self.assertEqual(len(self.db.scalars(select(SheetExportModel)).all()), 1)
        self.assertEqual(len(self.db.scalars(select(BidResultSnapshotModel)).all()), 1)
        self.assertEqual(len(self.db.scalars(select(BidNoticeEnrichmentJobModel)).all()), 1)

    def test_purge_keeps_records_at_the_thirty_day_boundary(self):
        boundary = self.now - timedelta(days=SOURCE_RETENTION_DAYS)
        self._add_pre_specification("BOUNDARY-PRE", boundary)
        self._add_notice("boundary-notice", boundary)
        self._add_opening_result("boundary-round", boundary)
        self.db.commit()

        result = purge_expired_source_data(self.db, now=self.now)

        self.assertEqual(result.pre_specification_count, 0)
        self.assertEqual(result.bid_notice_count, 0)
        self.assertEqual(result.opening_result_count, 0)
        self.assertEqual(self.db.query(PreSpecificationModel).count(), 1)
        self.assertEqual(self.db.query(ScraperNoticeModel).count(), 1)
        self.assertEqual(self.db.query(BidOpeningRoundModel).count(), 1)

    def _add_pre_specification(self, key, last_seen_at):
        self.db.add(
            PreSpecificationModel(
                bf_spec_rgst_no=key,
                raw_payload="{}",
                attachments_json="[]",
                first_seen_at=last_seen_at,
                last_seen_at=last_seen_at,
            )
        )

    def _add_notice(self, notice_id, last_seen_at):
        row = ScraperNoticeModel(
            dedup_key=notice_id,
            notice_id=notice_id,
            title=notice_id,
            source_payload="{}",
            first_seen_at=last_seen_at,
            last_seen_at=last_seen_at,
        )
        self.db.add(row)
        self.db.flush()
        return row.id

    def _add_opening_result(self, external_key, collected_at):
        row = BidOpeningRoundModel(
            external_key=external_key,
            bid_notice_no=external_key,
            collected_at=collected_at,
        )
        self.db.add(row)
        self.db.flush()
        return row.id

    def _add_related_rows(self, notice_id, pre_spec_key, round_id, result_key, timestamp):
        self.db.add_all(
            [
                PreSpecificationSnapshotModel(
                    bf_spec_rgst_no=pre_spec_key,
                    payload_hash=f"{pre_spec_key}-snapshot",
                    raw_payload="{}",
                    collected_at=timestamp,
                ),
                UserPreSpecificationStateModel(
                    organization_id=1,
                    user_id=1,
                    bf_spec_rgst_no=pre_spec_key,
                    state="EXPORTED",
                    acted_at=timestamp,
                ),
                PreSpecificationSheetExportModel(
                    destination_id=1,
                    organization_id=1,
                    bf_spec_rgst_no=pre_spec_key,
                    exported_by_user_id=1,
                    status="SUCCEEDED",
                    claimed_at=timestamp,
                ),
                BidNoticeDocumentAnalysisModel(
                    notice_id=notice_id,
                    attachment_key=f"{notice_id}-attachment",
                    attachment_name="공고문.pdf",
                    attachment_url="https://example.test/attachment",
                    analyzer_version="v1",
                ),
                UserBidNoticeMatchModel(
                    organization_id=1,
                    user_id=1,
                    notice_id=notice_id,
                    matched_at=timestamp,
                ),
                UserBidNoticeStateModel(
                    organization_id=1,
                    user_id=1,
                    notice_id=notice_id,
                    acted_at=timestamp,
                ),
                BidNoticeSheetExportModel(
                    destination_id=1,
                    organization_id=1,
                    user_id=1,
                    notice_id=notice_id,
                    status="SUCCEEDED",
                ),
                BidOpeningEntryModel(
                    round_id=round_id,
                    external_key=f"{result_key}-entry",
                ),
                OrganizationOpeningResultMatchModel(
                    organization_id=1,
                    round_id=round_id,
                    result_external_key=result_key,
                ),
                UserOpeningResultMatchModel(
                    organization_id=1,
                    user_id=1,
                    round_id=round_id,
                    result_external_key=result_key,
                ),
                UserOpeningResultStateModel(
                    organization_id=1,
                    user_id=1,
                    result_external_key=result_key,
                    state="EXPORTED",
                    acted_at=timestamp,
                ),
                SheetExportModel(
                    destination_id=1,
                    organization_id=1,
                    result_external_key=result_key,
                    exported_by_user_id=1,
                    status="SUCCEEDED",
                    claimed_at=timestamp,
                ),
                BidResultSnapshotModel(
                    entity_type="ROUND",
                    entity_key=f"{result_key}-snapshot",
                    payload_hash=f"{result_key}-hash",
                    raw_payload="{}",
                    collected_at=timestamp,
                ),
                BidNoticeEnrichmentJobModel(
                    bid_notice_no=f"{result_key}-notice",
                    bid_notice_ord="00",
                    task_type="NOTICE_CONTEXT",
                    updated_at=timestamp,
                ),
            ]
        )
