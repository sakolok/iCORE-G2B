import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import create_engine, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.data.models import Base, ScraperNoticeModel
from app.g2b.opening_results.enrichment_queue import (
    ENRICHMENT_PRIORITY_BUSINESS_AMOUNT,
    ENRICHMENT_PRIORITY_NOTICE_CONTEXT,
    enqueue_notice_enrichment_jobs,
    process_notice_enrichment_jobs,
)
from app.g2b.opening_results.models import (
    BidNoticeEnrichmentJobModel,
    BidOpeningRoundModel,
    UserOpeningResultMatchModel,
    UserOpeningResultStateModel,
)


class BidNoticeEnrichmentJobModelTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_notice_identity_and_task_type_are_unique(self):
        self.db.add(
            BidNoticeEnrichmentJobModel(
                bid_notice_no="R26BK00000001",
                bid_notice_ord="0",
                task_type="NOTICE_CONTEXT",
                priority=100,
            )
        )
        self.db.commit()

        stored = self.db.scalar(select(BidNoticeEnrichmentJobModel))
        self.assertIsNotNone(stored)
        self.assertEqual(stored.status, "PENDING")
        self.assertEqual(stored.retry_count, 0)

        self.db.add(
            BidNoticeEnrichmentJobModel(
                bid_notice_no="R26BK00000001",
                bid_notice_ord="0",
                task_type="NOTICE_CONTEXT",
            )
        )
        with self.assertRaises(IntegrityError):
            self.db.commit()

    def _add_matched_round(self, *, notice_no="R26BK00000001"):
        now = datetime.now(timezone.utc)
        round_row = BidOpeningRoundModel(
            external_key=f"SERVICE:{notice_no}:0:0:0",
            business_type="SERVICE",
            bid_notice_no=notice_no,
            bid_notice_ord="00",
            bid_class_no="0",
            rebid_no="0",
            title="클라우드 용역",
            status="OPENED",
            opened_at=now - timedelta(days=1),
            collected_at=now,
        )
        self.db.add(round_row)
        self.db.flush()
        self.db.add(
            UserOpeningResultMatchModel(
                organization_id=1,
                user_id=1,
                round_id=round_row.id,
                result_external_key=round_row.external_key,
                matched_keywords="클라우드",
                is_current_match=True,
            )
        )
        self.db.flush()
        return round_row

    def test_missing_business_amount_is_enqueued_once_with_high_priority(self):
        self._add_matched_round()

        self.assertEqual(enqueue_notice_enrichment_jobs(self.db), 1)
        self.assertEqual(enqueue_notice_enrichment_jobs(self.db), 0)

        stored = self.db.scalar(select(BidNoticeEnrichmentJobModel))
        self.assertEqual(stored.bid_notice_ord, "0")
        self.assertEqual(stored.priority, ENRICHMENT_PRIORITY_BUSINESS_AMOUNT)
        self.assertEqual(
            self.db.scalar(select(func.count(BidNoticeEnrichmentJobModel.id))),
            1,
        )

    def test_existing_business_amount_does_not_enqueue(self):
        now = datetime.now(timezone.utc)
        round_row = self._add_matched_round()
        self.db.add(
            ScraperNoticeModel(
                dedup_key="notice:R26BK00000001:0",
                notice_id="R26BK00000001",
                title=round_row.title,
                bid_notice_no=round_row.bid_notice_no,
                bid_notice_ord="0",
                business_name=round_row.title,
                demand_agency_name="OO기관",
                base_amount=Decimal("266264460"),
                proposal_deadline=now,
                region_restriction="서울특별시",
                region_restriction_api_status="API_VALUE",
                is_two_stage_bid=False,
                first_seen_at=now,
                last_seen_at=now,
            )
        )
        self.db.flush()

        self.assertEqual(enqueue_notice_enrichment_jobs(self.db), 0)

    def test_existing_api_error_is_enqueued_with_lower_priority(self):
        now = datetime.now(timezone.utc)
        round_row = self._add_matched_round()
        self.db.add(
            ScraperNoticeModel(
                dedup_key="notice:R26BK00000001:0",
                notice_id="R26BK00000001",
                title=round_row.title,
                bid_notice_no=round_row.bid_notice_no,
                bid_notice_ord="0",
                business_name=round_row.title,
                demand_agency_name="OO기관",
                base_amount=Decimal("266264460"),
                proposal_deadline=now,
                region_restriction_api_status="API_ERROR",
                is_two_stage_bid=False,
                first_seen_at=now,
                last_seen_at=now,
            )
        )
        self.db.flush()

        self.assertEqual(enqueue_notice_enrichment_jobs(self.db), 1)
        job = self.db.scalar(select(BidNoticeEnrichmentJobModel))
        self.assertEqual(job.priority, ENRICHMENT_PRIORITY_NOTICE_CONTEXT)

    def test_existing_api_empty_region_alone_does_not_enqueue(self):
        now = datetime.now(timezone.utc)
        round_row = self._add_matched_round()
        self.db.add(
            ScraperNoticeModel(
                dedup_key="notice:R26BK00000001:0",
                notice_id="R26BK00000001",
                title=round_row.title,
                bid_notice_no=round_row.bid_notice_no,
                bid_notice_ord="0",
                business_name=round_row.title,
                demand_agency_name="OO기관",
                base_amount=Decimal("266264460"),
                proposal_deadline=now,
                region_restriction_api_status="API_EMPTY",
                is_two_stage_bid=False,
                first_seen_at=now,
                last_seen_at=now,
            )
        )
        self.db.flush()

        self.assertEqual(enqueue_notice_enrichment_jobs(self.db), 0)

    def test_terminal_user_result_does_not_enqueue(self):
        round_row = self._add_matched_round()
        self.db.add(
            UserOpeningResultStateModel(
                organization_id=1,
                user_id=1,
                result_external_key=round_row.external_key,
                state="EXPORTED",
            )
        )
        self.db.flush()

        self.assertEqual(enqueue_notice_enrichment_jobs(self.db), 0)

    def _add_enrichment_job(self, notice_no="R26BK00000001"):
        now = datetime.now(timezone.utc)
        round_row = BidOpeningRoundModel(
            external_key=f"SERVICE:{notice_no}:0:0:0",
            business_type="SERVICE",
            bid_notice_no=notice_no,
            bid_notice_ord="00",
            bid_class_no="0",
            rebid_no="0",
            title="클라우드 용역",
            status="OPENED",
            opened_at=now,
            collected_at=now,
        )
        job = BidNoticeEnrichmentJobModel(
            bid_notice_no=notice_no,
            bid_notice_ord="0",
            task_type="NOTICE_CONTEXT",
            status="PENDING",
            priority=100,
        )
        self.db.add_all([round_row, job])
        self.db.commit()
        return round_row, job

    def _store_context(
        self,
        round_row,
        *,
        region_status,
        region_restriction=None,
    ):
        context = self.db.scalar(
            select(ScraperNoticeModel).where(
                ScraperNoticeModel.bid_notice_no == round_row.bid_notice_no
            )
        )
        if context is None:
            now = datetime.now(timezone.utc)
            context = ScraperNoticeModel(
                dedup_key=f"notice:{round_row.bid_notice_no}:0",
                notice_id=round_row.bid_notice_no,
                title=round_row.title,
                bid_notice_no=round_row.bid_notice_no,
                bid_notice_ord="0",
                first_seen_at=now,
                last_seen_at=now,
            )
            self.db.add(context)
        context.base_amount = Decimal("266264460")
        context.business_name = round_row.title
        context.demand_agency_name = "OO기관"
        context.proposal_deadline = datetime.now(timezone.utc)
        context.is_two_stage_bid = False
        context.region_restriction = region_restriction
        context.region_restriction_api_status = region_status
        self.db.flush()

    def test_api_empty_becomes_needs_review_without_requeue(self):
        round_row, job = self._add_enrichment_job()
        calls = []

        def enrich(db, selected_round):
            calls.append(selected_round.bid_notice_no)
            self._store_context(selected_round, region_status="API_EMPTY")
            return 1

        now = datetime(2026, 7, 22, 0, 0, tzinfo=timezone.utc)
        result = process_notice_enrichment_jobs(
            self.db,
            now=now,
            enrich_notice_context=enrich,
        )
        self.db.refresh(job)

        self.assertEqual(result.needs_review_count, 1)
        self.assertEqual(job.status, "NEEDS_REVIEW")
        self.assertEqual(job.last_error, "REGION_API_EMPTY")
        self.assertEqual(job.retry_count, 0)
        self.assertIsNone(job.next_retry_at)
        self.assertEqual(
            self.db.scalar(select(ScraperNoticeModel.base_amount)),
            Decimal("266264460"),
        )

        second = process_notice_enrichment_jobs(
            self.db,
            now=now + timedelta(days=1),
            enrich_notice_context=enrich,
        )
        self.assertEqual(second.claimed_count, 0)
        self.assertEqual(calls, [round_row.bid_notice_no])

    def test_api_error_retries_only_after_next_retry_at(self):
        _, job = self._add_enrichment_job()
        calls = 0

        def enrich(db, selected_round):
            nonlocal calls
            calls += 1
            if calls == 1:
                self._store_context(selected_round, region_status="API_ERROR")
            else:
                self._store_context(
                    selected_round,
                    region_status="API_VALUE",
                    region_restriction="경기도",
                )
            return 1

        now = datetime(2026, 7, 22, 0, 0, tzinfo=timezone.utc)
        first = process_notice_enrichment_jobs(
            self.db,
            now=now,
            enrich_notice_context=enrich,
        )
        self.db.refresh(job)
        self.assertEqual(first.retry_scheduled_count, 1)
        self.assertEqual(job.status, "RETRY_WAIT")
        self.assertEqual(job.retry_count, 1)

        not_due = process_notice_enrichment_jobs(
            self.db,
            now=now + timedelta(minutes=59),
            enrich_notice_context=enrich,
        )
        self.assertEqual(not_due.claimed_count, 0)
        self.assertEqual(calls, 1)

        due = process_notice_enrichment_jobs(
            self.db,
            now=now + timedelta(hours=1),
            enrich_notice_context=enrich,
        )
        self.db.refresh(job)
        self.assertEqual(due.succeeded_count, 1)
        self.assertEqual(job.status, "SUCCEEDED")
        self.assertEqual(calls, 2)

    def test_order_mismatch_needs_review_without_retry(self):
        _, job = self._add_enrichment_job()

        def enrich(db, selected_round):
            self._store_context(
                selected_round,
                region_status="ORDER_MISMATCH",
            )
            return 1

        result = process_notice_enrichment_jobs(
            self.db,
            now=datetime(2026, 7, 22, 0, 0, tzinfo=timezone.utc),
            enrich_notice_context=enrich,
        )
        self.db.refresh(job)

        self.assertEqual(result.needs_review_count, 1)
        self.assertEqual(result.retry_scheduled_count, 0)
        self.assertEqual(job.status, "NEEDS_REVIEW")
        self.assertEqual(job.last_error, "REGION_API_ORDER_MISMATCH")
        self.assertEqual(job.retry_count, 0)

    def test_existing_missing_amount_retries_when_context_api_fails(self):
        round_row, job = self._add_enrichment_job()
        now = datetime.now(timezone.utc)
        self.db.add(
            ScraperNoticeModel(
                dedup_key="notice:R26BK00000001:0",
                notice_id=round_row.bid_notice_no,
                title=round_row.title,
                bid_notice_no=round_row.bid_notice_no,
                bid_notice_ord="0",
                region_restriction_api_status="API_EMPTY",
                first_seen_at=now,
                last_seen_at=now,
            )
        )
        self.db.commit()

        result = process_notice_enrichment_jobs(
            self.db,
            now=datetime(2026, 7, 22, 0, 0, tzinfo=timezone.utc),
            enrich_notice_context=lambda db, selected_round: 0,
        )
        self.db.refresh(job)

        self.assertEqual(result.retry_scheduled_count, 1)
        self.assertEqual(job.status, "RETRY_WAIT")
        self.assertEqual(job.last_error, "NOTICE_CONTEXT_API_ERROR")

    def test_successful_context_without_amount_needs_review(self):
        round_row, job = self._add_enrichment_job()

        def enrich(db, selected_round):
            now = datetime.now(timezone.utc)
            self.db.add(
                ScraperNoticeModel(
                    dedup_key="notice:R26BK00000001:0",
                    notice_id=selected_round.bid_notice_no,
                    title=selected_round.title,
                    bid_notice_no=selected_round.bid_notice_no,
                    bid_notice_ord="0",
                    region_restriction="서울특별시",
                    region_restriction_api_status="API_VALUE",
                    first_seen_at=now,
                    last_seen_at=now,
                )
            )
            self.db.flush()
            return 1

        result = process_notice_enrichment_jobs(
            self.db,
            now=datetime(2026, 7, 22, 0, 0, tzinfo=timezone.utc),
            enrich_notice_context=enrich,
        )
        self.db.refresh(job)

        self.assertEqual(result.needs_review_count, 1)
        self.assertEqual(result.retry_scheduled_count, 0)
        self.assertEqual(job.status, "NEEDS_REVIEW")
        self.assertEqual(job.last_error, "BUSINESS_AMOUNT_EMPTY")

    def test_other_missing_context_field_needs_review(self):
        _, job = self._add_enrichment_job()

        def enrich(db, selected_round):
            self._store_context(
                selected_round,
                region_status="API_VALUE",
                region_restriction="서울특별시",
            )
            context = self.db.scalar(select(ScraperNoticeModel))
            context.proposal_deadline = None
            self.db.flush()
            return 1

        result = process_notice_enrichment_jobs(
            self.db,
            now=datetime(2026, 7, 22, 0, 0, tzinfo=timezone.utc),
            enrich_notice_context=enrich,
        )
        self.db.refresh(job)

        self.assertEqual(result.needs_review_count, 1)
        self.assertEqual(job.status, "NEEDS_REVIEW")
        self.assertEqual(
            job.last_error,
            "NOTICE_CONTEXT_INCOMPLETE:proposal_deadline",
        )

    def test_each_job_commits_independently(self):
        _, failed_job = self._add_enrichment_job("R26BK00000001")
        _, successful_job = self._add_enrichment_job("R26BK00000002")

        def enrich(db, selected_round):
            if selected_round.bid_notice_no == failed_job.bid_notice_no:
                raise RuntimeError("temporary failure")
            self._store_context(
                selected_round,
                region_status="API_VALUE",
                region_restriction="서울특별시",
            )
            return 1

        result = process_notice_enrichment_jobs(
            self.db,
            now=datetime(2026, 7, 22, 0, 0, tzinfo=timezone.utc),
            enrich_notice_context=enrich,
        )
        self.db.refresh(failed_job)
        self.db.refresh(successful_job)

        self.assertEqual(result.claimed_count, 2)
        self.assertEqual(result.retry_scheduled_count, 1)
        self.assertEqual(result.succeeded_count, 1)
        self.assertEqual(failed_job.status, "RETRY_WAIT")
        self.assertEqual(successful_job.status, "SUCCEEDED")


if __name__ == "__main__":
    unittest.main()
