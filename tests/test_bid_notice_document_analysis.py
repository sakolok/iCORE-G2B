import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.data.models import Base, ScraperNoticeModel
from app.g2b.bid_notice import REGION_API_EMPTY, REGION_API_VALUE
from app.g2b.bid_notices.collector import INDUSTRY_API_EMPTY, INDUSTRY_API_VALUE
from app.g2b.bid_notices.document_analysis import (
    run_pending_bid_notice_document_analysis,
)
from app.g2b.bid_notices.matching import (
    sync_user_bid_notice_matches,
    update_user_bid_notice_profile,
)
from app.g2b.bid_notices.models import BidNoticeDocumentAnalysisModel


class BidNoticeDocumentAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def _add_matched_notice(self):
        now = datetime.now(timezone.utc)
        notice = ScraperNoticeModel(
            dedup_key="document-analysis-test",
            notice_id="R26BK000100",
            bid_notice_no="R26BK000100",
            bid_notice_ord="000",
            title="AI 문서 분석 공고",
            business_name="AI 문서 분석 공고",
            work_type="용역",
            published_at=now,
            first_seen_at=now,
            last_seen_at=now,
            source_payload=(
                '{"ntceSpecFileNm1":"제안요청서.pdf",'
                '"ntceSpecDocUrl1":"https://www.g2b.go.kr/file/request.pdf"}'
            ),
        )
        self.db.add(notice)
        self.db.commit()
        update_user_bid_notice_profile(
            self.db,
            organization_id=1,
            user_id=10,
            enabled=True,
            keywords=["AI"],
            excluded_keywords=[],
        )
        sync_user_bid_notice_matches(self.db, organization_id=1, user_id=10, now=now)
        self.db.commit()
        return notice

    @patch("app.g2b.bid_notices.document_analysis._extract_text")
    @patch("app.g2b.bid_notices.document_analysis._download_attachment")
    @patch("app.g2b.bid_notices.document_analysis.fetch_industry_restriction_codes")
    @patch("app.g2b.bid_notices.document_analysis.fetch_participant_region_restriction")
    def test_document_analysis_fills_only_api_missing_fields(
        self,
        fetch_region,
        fetch_industry,
        download_attachment,
        extract_text,
    ):
        notice = self._add_matched_notice()
        fetch_region.return_value = (None, REGION_API_EMPTY)
        fetch_industry.return_value = (None, INDUSTRY_API_EMPTY)
        download_attachment.return_value = (b"pdf", "application/pdf")
        extract_text.return_value = (
            "입찰참가자격 지역제한 충청북도 주된 영업소 소재지\n"
            "사업자등록 업종코드 1169 보유 업체"
        )

        result = run_pending_bid_notice_document_analysis(self.db)

        stored = self.db.get(ScraperNoticeModel, notice.id)
        analysis = self.db.scalar(select(BidNoticeDocumentAnalysisModel))
        self.assertEqual(result["analyzed_count"], 1)
        self.assertEqual(stored.region_restriction, "충청북도")
        self.assertEqual(stored.region_restriction_api_status, "DOCUMENT_VALUE")
        self.assertEqual(stored.region_restriction_source, "DOCUMENT")
        self.assertEqual(stored.industry_restriction_codes, "1169")
        self.assertEqual(stored.industry_restriction_api_status, "DOCUMENT_VALUE")
        self.assertEqual(stored.industry_restriction_source, "DOCUMENT")
        self.assertEqual(analysis.status, "SUCCEEDED")
        self.assertIn("업종코드 1169", analysis.evidence)

    @patch("app.g2b.bid_notices.document_analysis._download_attachment")
    @patch("app.g2b.bid_notices.document_analysis.fetch_industry_restriction_codes")
    @patch("app.g2b.bid_notices.document_analysis.fetch_participant_region_restriction")
    def test_document_analysis_skips_api_confirmed_notice(
        self,
        fetch_region,
        fetch_industry,
        download_attachment,
    ):
        self._add_matched_notice()
        fetch_region.return_value = ("충청북도", REGION_API_VALUE)
        fetch_industry.return_value = ("1169", INDUSTRY_API_VALUE)

        result = run_pending_bid_notice_document_analysis(self.db)

        self.assertEqual(result["candidate_count"], 1)
        self.assertEqual(result["queued_count"], 0)
        self.assertIsNone(self.db.scalar(select(BidNoticeDocumentAnalysisModel)))
        download_attachment.assert_not_called()

    @patch("app.g2b.bid_notices.document_analysis._extract_text")
    @patch("app.g2b.bid_notices.document_analysis._download_attachment")
    @patch("app.g2b.bid_notices.document_analysis.fetch_industry_restriction_codes")
    @patch("app.g2b.bid_notices.document_analysis.fetch_participant_region_restriction")
    def test_document_analysis_does_not_turn_missing_code_into_none(
        self,
        fetch_region,
        fetch_industry,
        download_attachment,
        extract_text,
    ):
        notice = self._add_matched_notice()
        fetch_region.return_value = (None, REGION_API_EMPTY)
        fetch_industry.return_value = (None, INDUSTRY_API_EMPTY)
        download_attachment.return_value = (b"pdf", "application/pdf")
        extract_text.return_value = "입찰 관련 일반 안내문"

        result = run_pending_bid_notice_document_analysis(self.db)

        stored = self.db.get(ScraperNoticeModel, notice.id)
        analysis = self.db.scalar(select(BidNoticeDocumentAnalysisModel))
        self.assertEqual(result["review_required_count"], 1)
        self.assertIsNone(stored.industry_restriction_codes)
        self.assertEqual(stored.industry_restriction_api_status, INDUSTRY_API_EMPTY)
        self.assertEqual(analysis.status, "REVIEW_REQUIRED")

    @patch("app.g2b.bid_notices.document_analysis._extract_text")
    @patch("app.g2b.bid_notices.document_analysis._download_attachment")
    @patch("app.g2b.bid_notices.document_analysis.fetch_industry_restriction_codes")
    @patch("app.g2b.bid_notices.document_analysis.fetch_participant_region_restriction")
    def test_document_analysis_keeps_explicit_none_separate_from_api_empty(
        self,
        fetch_region,
        fetch_industry,
        download_attachment,
        extract_text,
    ):
        notice = self._add_matched_notice()
        fetch_region.return_value = (None, REGION_API_EMPTY)
        fetch_industry.return_value = (None, INDUSTRY_API_EMPTY)
        download_attachment.return_value = (b"pdf", "application/pdf")
        extract_text.return_value = "지역제한 없음\n업종제한 없음"

        run_pending_bid_notice_document_analysis(self.db)

        stored = self.db.get(ScraperNoticeModel, notice.id)
        self.assertEqual(stored.region_restriction, "해당없음")
        self.assertEqual(stored.region_restriction_api_status, "DOCUMENT_NONE")
        self.assertEqual(stored.industry_restriction_api_status, "DOCUMENT_NONE")
