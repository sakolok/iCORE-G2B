import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import requests
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.data.models import Base, ScraperNoticeModel
from app.g2b.bid_notice import REGION_API_EMPTY, REGION_API_ERROR, REGION_API_VALUE
from app.g2b.bid_notices.collector import (
    INDUSTRY_API_EMPTY,
    INDUSTRY_API_ERROR,
    INDUSTRY_API_VALUE,
)
from app.g2b.bid_notices.document_analysis import (
    AttachmentDownloadError,
    DOWNLOAD_CONNECT_TIMEOUT_SECONDS,
    MAX_ANALYSIS_ATTEMPTS,
    _analyze_text,
    _document_fetcher_enabled_for,
    _retry_at,
    _download_attachment,
    _download_attachment_via_fetcher,
    force_bid_notice_document_reanalysis,
    queue_new_matched_bid_notice_document_preparations,
    run_pending_bid_notice_document_analysis,
)
from app.g2b.bid_notices.matching import (
    sync_user_bid_notice_matches,
    update_user_bid_notice_profile,
)
from app.g2b.bid_notices.models import (
    BidNoticeDocumentAnalysisModel,
)
from app.g2b.bid_notices.router import _notice_response
from app.g2b.bid_notices.sheet_export import build_bid_notice_sheet_rows


class _DownloadResponse:
    def __init__(
        self,
        *,
        status_code: int = 200,
        url: str = "https://www.g2b.go.kr/file/notice.pdf",
        headers: dict[str, str] | None = None,
        content: bytes = b"pdf",
    ):
        self.status_code = status_code
        self.url = url
        self.headers = headers or {"Content-Type": "application/pdf"}
        self._content = content
        self.content = content
        self.closed = False

    def raise_for_status(self):
        if self.status_code >= 400:
            error = requests.HTTPError(f"HTTP {self.status_code}")
            error.response = self
            raise error

    def iter_content(self, chunk_size: int):
        del chunk_size
        yield self._content

    def close(self):
        self.closed = True


class BidNoticeDocumentAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine, autoflush=False)
        self.now = datetime(2026, 7, 29, 9, tzinfo=timezone.utc)

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_attachment_retry_policy_uses_two_retries_and_short_delays(self):
        self.assertEqual(DOWNLOAD_CONNECT_TIMEOUT_SECONDS, 15)
        self.assertEqual(MAX_ANALYSIS_ATTEMPTS, 3)
        self.assertEqual(_retry_at(self.now, 1), self.now + timedelta(minutes=5))
        self.assertEqual(_retry_at(self.now, 2), self.now + timedelta(minutes=10))
        self.assertIsNone(_retry_at(self.now, 3))

    def test_industry_code_analysis_ignores_years_and_phone_numbers(self):
        findings = _analyze_text(
            "사업자등록증 사본을 제출합니다. 입찰번호 관재2026-12. "
            "전화 063-450-7063, 팩스 450-7777. 관련기본법에 따라 등록한 업체."
        )

        self.assertIsNone(findings["industry_codes"])
        self.assertIsNone(findings["industry_status"])

    def test_industry_code_analysis_accepts_explicit_code_label(self):
        findings = _analyze_text(
            "입찰참가자격은 업종제한 코드 0036, 1468을 등록한 업체입니다."
        )

        self.assertEqual(findings["industry_codes"], "0036, 1468")
        self.assertEqual(findings["industry_status"], "DOCUMENT_VALUE")

    def test_document_fetcher_rollout_can_target_one_notice(self):
        notice = self._add_matched_notice()
        with patch.dict(
            "os.environ",
            {
                "G2B_DOCUMENT_FETCHER_ENABLED": "true",
                "G2B_DOCUMENT_FETCHER_URL": "https://fetcher.example.run.app",
                "G2B_DOCUMENT_FETCHER_NOTICE_IDS": "R26BK000001-000",
            },
            clear=False,
        ):
            self.assertTrue(_document_fetcher_enabled_for(notice))

        with patch.dict(
            "os.environ",
            {
                "G2B_DOCUMENT_FETCHER_ENABLED": "false",
                "G2B_DOCUMENT_FETCHER_URL": "https://fetcher.example.run.app",
                "G2B_DOCUMENT_FETCHER_NOTICE_IDS": "R26BK000001-000",
            },
            clear=False,
        ):
            self.assertFalse(_document_fetcher_enabled_for(notice))

    @patch("app.g2b.bid_notices.document_analysis.requests.post")
    @patch("app.g2b.bid_notices.document_analysis.google_id_token.fetch_id_token")
    def test_document_fetcher_download_uses_authenticated_service(
        self, fetch_id_token, requests_post
    ):
        fetch_id_token.return_value = "identity-token"
        response = _DownloadResponse(content=b"proxied-pdf")
        requests_post.return_value = response
        with patch.dict(
            "os.environ",
            {
                "G2B_DOCUMENT_FETCHER_URL": "https://fetcher.example.run.app",
                "G2B_DOCUMENT_FETCHER_AUDIENCE": "https://fetcher.example.run.app",
            },
            clear=False,
        ):
            content, content_type = _download_attachment_via_fetcher(
                "https://www.g2b.go.kr/file/notice.pdf"
            )

        self.assertEqual(content, b"proxied-pdf")
        self.assertEqual(content_type, "application/pdf")
        fetch_id_token.assert_called_once()
        self.assertEqual(
            requests_post.call_args.kwargs["headers"]["Authorization"],
            "Bearer identity-token",
        )
        self.assertTrue(response.closed)

    def _add_matched_notice(self, index: int = 1) -> ScraperNoticeModel:
        notice = ScraperNoticeModel(
            dedup_key=f"document-analysis-test-{index}",
            notice_id=f"R26BK000{index:03d}",
            bid_notice_no=f"R26BK000{index:03d}",
            bid_notice_ord="000",
            title=f"AI 문서 분석 공고 {index}",
            business_name=f"AI 문서 분석 공고 {index}",
            work_type="용역",
            published_at=self.now,
            first_seen_at=self.now,
            last_seen_at=self.now,
            source_payload=(
                "{"
                '"ntceSpecFileNm1":"제안요청서.pdf",'
                f'"ntceSpecDocUrl1":"https://www.g2b.go.kr/file/request-{index}.pdf",'
                '"ntceSpecFileNm2":"입찰공고안.pdf",'
                f'"ntceSpecDocUrl2":"https://www.g2b.go.kr/file/notice-{index}.pdf",'
                '"ntceSpecFileNm3":"물품규격서.pdf",'
                f'"ntceSpecDocUrl3":"https://www.g2b.go.kr/file/spec-{index}.pdf"'
                "}"
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
        sync_user_bid_notice_matches(self.db, organization_id=1, user_id=10, now=self.now)
        self.db.commit()
        return notice

    def _queue_for_document_analysis(self, *notices: ScraperNoticeModel) -> int:
        queued = queue_new_matched_bid_notice_document_preparations(
            self.db,
            notice_ids=[notice.id for notice in notices],
        )
        self.db.commit()
        return queued

    @patch("app.g2b.bid_notices.document_analysis.requests.get")
    def test_download_follows_only_g2b_redirects(self, requests_get):
        redirect = _DownloadResponse(
            status_code=302,
            headers={"Location": "/file/final-notice.pdf"},
        )
        complete = _DownloadResponse(
            url="https://www.g2b.go.kr/file/final-notice.pdf",
            content=b"final-pdf",
        )
        requests_get.side_effect = [redirect, complete]

        content, content_type = _download_attachment("https://www.g2b.go.kr/file/start.pdf")

        self.assertEqual(content, b"final-pdf")
        self.assertEqual(content_type, "application/pdf")
        self.assertTrue(redirect.closed)
        self.assertTrue(complete.closed)
        self.assertEqual(requests_get.call_count, 2)
        self.assertFalse(requests_get.call_args.kwargs["allow_redirects"])
        self.assertEqual(requests_get.call_args.kwargs["timeout"], (15, 25))
        self.assertIn("User-Agent", requests_get.call_args.kwargs["headers"])

    @patch("app.g2b.bid_notices.document_analysis.requests.get")
    def test_download_rejects_non_g2b_redirect(self, requests_get):
        redirect = _DownloadResponse(
            status_code=302,
            headers={"Location": "https://example.com/file.pdf"},
        )
        requests_get.return_value = redirect

        with self.assertRaises(AttachmentDownloadError) as raised:
            _download_attachment("https://www.g2b.go.kr/file/start.pdf")

        self.assertEqual(raised.exception.code, "DOWNLOAD_REDIRECT_BLOCKED")
        self.assertFalse(raised.exception.retryable)
        self.assertTrue(redirect.closed)

    @patch("app.g2b.bid_notices.document_analysis.time.sleep")
    @patch("app.g2b.bid_notices.document_analysis.requests.get")
    def test_download_retries_transient_connection_error(self, requests_get, sleep):
        complete = _DownloadResponse(content=b"final-pdf")
        requests_get.side_effect = [requests.ConnectTimeout("temporary"), complete]

        content, _ = _download_attachment("https://www.g2b.go.kr/file/start.pdf")

        self.assertEqual(content, b"final-pdf")
        self.assertEqual(requests_get.call_count, 2)
        sleep.assert_called_once_with(1)

    @patch("app.g2b.bid_notices.document_analysis.requests.get")
    def test_download_does_not_retry_not_found_attachment(self, requests_get):
        requests_get.return_value = _DownloadResponse(status_code=404)

        with self.assertRaises(AttachmentDownloadError) as raised:
            _download_attachment("https://www.g2b.go.kr/file/missing.pdf")

        self.assertEqual(raised.exception.code, "DOWNLOAD_HTTP_404")
        self.assertFalse(raised.exception.retryable)
        self.assertEqual(requests_get.call_count, 1)

    @patch("app.g2b.bid_notices.document_analysis._extract_text")
    @patch("app.g2b.bid_notices.document_analysis._download_attachment")
    @patch("app.g2b.bid_notices.document_analysis.fetch_explicit_region_restriction")
    @patch("app.g2b.bid_notices.document_analysis.fetch_industry_restriction_codes")
    @patch("app.g2b.bid_notices.document_analysis.fetch_participant_region_restriction")
    def test_analyzes_only_primary_notice_document_for_api_empty_fields(
        self,
        fetch_region,
        fetch_industry,
        fetch_explicit_region,
        download_attachment,
        extract_text,
    ):
        notice = self._add_matched_notice()
        fetch_region.return_value = (None, REGION_API_EMPTY)
        fetch_explicit_region.return_value = (None, None)
        fetch_industry.return_value = (None, INDUSTRY_API_EMPTY)
        download_attachment.return_value = (b"pdf", "application/pdf")
        extract_text.return_value = (
            "입찰참가자격 지역제한 충청북도 주된 영업소 소재지\n"
            "사업자등록 업종코드 1169 보유 업체"
        )

        self.assertEqual(self._queue_for_document_analysis(notice), 1)
        result = run_pending_bid_notice_document_analysis(self.db, now=self.now)

        stored = self.db.get(ScraperNoticeModel, notice.id)
        analysis = self.db.scalar(select(BidNoticeDocumentAnalysisModel))
        self.assertEqual(result["claimed_count"], 1)
        self.assertEqual(result["analyzed_count"], 1)
        self.assertEqual(stored.region_restriction, "충청북도")
        self.assertEqual(stored.region_restriction_api_status, "DOCUMENT_VALUE")
        self.assertEqual(stored.industry_restriction_codes, "1169")
        self.assertEqual(stored.industry_restriction_api_status, "DOCUMENT_VALUE")
        self.assertTrue(stored.icore_industry_code_match)
        self.assertTrue(analysis.is_primary_notice_document)
        self.assertEqual(analysis.attachment_name, "입찰공고안.pdf")
        download_attachment.assert_called_once_with("https://www.g2b.go.kr/file/notice-1.pdf")
        self.assertEqual(
            _notice_response(stored, "AI").region_restriction,
            "충청북도 (문서분석)",
        )
        self.assertEqual(
            build_bid_notice_sheet_rows([stored])[0][5],
            "1169 (문서분석)",
        )

    @patch("app.g2b.bid_notices.document_analysis._download_attachment")
    @patch("app.g2b.bid_notices.document_analysis.fetch_industry_restriction_codes")
    @patch("app.g2b.bid_notices.document_analysis.fetch_participant_region_restriction")
    def test_skips_document_analysis_when_api_confirms_fields(
        self,
        fetch_region,
        fetch_industry,
        download_attachment,
    ):
        notice = self._add_matched_notice()
        fetch_region.return_value = ("충청북도", REGION_API_VALUE)
        fetch_industry.return_value = ("1169", INDUSTRY_API_VALUE)

        self.assertEqual(self._queue_for_document_analysis(notice), 1)
        result = run_pending_bid_notice_document_analysis(self.db, now=self.now)

        self.assertEqual(result["candidate_count"], 1)
        self.assertEqual(result["queued_count"], 0)
        self.assertEqual(result["claimed_count"], 0)
        self.assertIsNone(self.db.scalar(select(BidNoticeDocumentAnalysisModel)))
        download_attachment.assert_not_called()

    @patch("app.g2b.bid_notices.document_analysis._download_attachment")
    @patch("app.g2b.bid_notices.document_analysis.fetch_industry_restriction_codes")
    @patch("app.g2b.bid_notices.document_analysis.fetch_participant_region_restriction")
    def test_does_not_use_document_analysis_for_api_errors(
        self,
        fetch_region,
        fetch_industry,
        download_attachment,
    ):
        notice = self._add_matched_notice()
        fetch_region.return_value = (None, REGION_API_ERROR)
        fetch_industry.return_value = (None, INDUSTRY_API_ERROR)

        self.assertEqual(self._queue_for_document_analysis(notice), 1)
        result = run_pending_bid_notice_document_analysis(self.db, now=self.now)

        self.assertEqual(result["queued_count"], 0)
        self.assertEqual(result["claimed_count"], 0)
        self.assertIsNone(self.db.scalar(select(BidNoticeDocumentAnalysisModel)))
        download_attachment.assert_not_called()

    @patch("app.g2b.bid_notices.document_analysis._extract_text")
    @patch("app.g2b.bid_notices.document_analysis._download_attachment")
    @patch("app.g2b.bid_notices.document_analysis.fetch_explicit_region_restriction")
    @patch("app.g2b.bid_notices.document_analysis.fetch_industry_restriction_codes")
    @patch("app.g2b.bid_notices.document_analysis.fetch_participant_region_restriction")
    def test_keeps_document_confirmation_required_when_no_value_is_found(
        self,
        fetch_region,
        fetch_industry,
        fetch_explicit_region,
        download_attachment,
        extract_text,
    ):
        notice = self._add_matched_notice()
        fetch_region.return_value = (None, REGION_API_EMPTY)
        fetch_explicit_region.return_value = (None, None)
        fetch_industry.return_value = (None, INDUSTRY_API_EMPTY)
        download_attachment.return_value = (b"pdf", "application/pdf")
        extract_text.return_value = "입찰 관련 일반 안내문"

        self.assertEqual(self._queue_for_document_analysis(notice), 1)
        result = run_pending_bid_notice_document_analysis(self.db, now=self.now)

        stored = self.db.get(ScraperNoticeModel, notice.id)
        analysis = self.db.scalar(select(BidNoticeDocumentAnalysisModel))
        self.assertEqual(result["review_required_count"], 1)
        self.assertIsNone(stored.industry_restriction_codes)
        self.assertEqual(stored.industry_restriction_api_status, INDUSTRY_API_EMPTY)
        self.assertEqual(analysis.status, "REVIEW_REQUIRED")
        self.assertEqual(analysis.error_message, "관련 문구 미검출")
        response = _notice_response(stored, "AI", document_analysis=analysis)
        self.assertEqual(response.document_analysis_reason, "관련 문구 미검출")

    @patch("app.g2b.bid_notices.document_analysis._extract_text")
    @patch("app.g2b.bid_notices.document_analysis._download_attachment")
    @patch("app.g2b.bid_notices.document_analysis.fetch_explicit_region_restriction")
    @patch("app.g2b.bid_notices.document_analysis.fetch_industry_restriction_codes")
    @patch("app.g2b.bid_notices.document_analysis.fetch_participant_region_restriction")
    def test_processes_at_most_ten_notices_per_worker_run(
        self,
        fetch_region,
        fetch_industry,
        fetch_explicit_region,
        download_attachment,
        extract_text,
    ):
        notices = [self._add_matched_notice(index) for index in range(1, 13)]
        fetch_region.return_value = (None, REGION_API_EMPTY)
        fetch_explicit_region.return_value = (None, None)
        fetch_industry.return_value = (None, INDUSTRY_API_EMPTY)
        download_attachment.return_value = (b"pdf", "application/pdf")
        extract_text.return_value = "지역제한 없음\n업종제한 없음"

        self.assertEqual(self._queue_for_document_analysis(*notices), 12)

        first = run_pending_bid_notice_document_analysis(
            self.db, now=self.now, batch_size=10
        )
        second = run_pending_bid_notice_document_analysis(
            self.db, now=self.now + timedelta(minutes=1), batch_size=10
        )

        self.assertEqual(first["claimed_count"], 10)
        self.assertEqual(first["analyzed_count"], 10)
        self.assertEqual(second["claimed_count"], 2)
        self.assertEqual(second["analyzed_count"], 2)
        self.assertEqual(download_attachment.call_count, 12)

    @patch("app.g2b.bid_notices.document_analysis._extract_text")
    @patch("app.g2b.bid_notices.document_analysis._download_attachment")
    @patch("app.g2b.bid_notices.document_analysis.fetch_explicit_region_restriction")
    @patch("app.g2b.bid_notices.document_analysis.fetch_industry_restriction_codes")
    @patch("app.g2b.bid_notices.document_analysis.fetch_participant_region_restriction")
    def test_worker_run_claims_a_prepared_queue_without_rematching(
        self,
        fetch_region,
        fetch_industry,
        fetch_explicit_region,
        download_attachment,
        extract_text,
    ):
        queued_notice = self._add_matched_notice()
        unqueued_notice = self._add_matched_notice(2)
        fetch_region.return_value = (None, REGION_API_EMPTY)
        fetch_explicit_region.return_value = (None, None)
        fetch_industry.return_value = (None, INDUSTRY_API_EMPTY)
        download_attachment.return_value = (b"pdf", "application/pdf")
        extract_text.return_value = "지역제한 없음\n업종제한 없음"

        self.assertEqual(self._queue_for_document_analysis(queued_notice), 1)
        result = run_pending_bid_notice_document_analysis(
            self.db,
            now=self.now,
            prepare_queue=False,
        )

        self.assertEqual(result["candidate_count"], 1)
        self.assertEqual(result["queued_count"], 1)
        self.assertEqual(result["claimed_count"], 1)
        self.assertEqual(result["analyzed_count"], 1)
        self.assertIsNone(
            self.db.scalar(
                select(BidNoticeDocumentAnalysisModel).where(
                    BidNoticeDocumentAnalysisModel.notice_id == unqueued_notice.id
                )
            )
        )

    @patch("app.g2b.bid_notices.document_analysis._extract_text")
    @patch("app.g2b.bid_notices.document_analysis._download_attachment")
    @patch("app.g2b.bid_notices.document_analysis.fetch_explicit_region_restriction")
    @patch("app.g2b.bid_notices.document_analysis.fetch_industry_restriction_codes")
    @patch("app.g2b.bid_notices.document_analysis.fetch_participant_region_restriction")
    def test_targeted_reanalysis_only_claims_the_requested_notice(
        self,
        fetch_region,
        fetch_industry,
        fetch_explicit_region,
        download_attachment,
        extract_text,
    ):
        target_notice = self._add_matched_notice()
        other_notice = self._add_matched_notice(2)
        fetch_region.return_value = (None, REGION_API_EMPTY)
        fetch_explicit_region.return_value = (None, None)
        fetch_industry.return_value = (None, INDUSTRY_API_EMPTY)
        download_attachment.return_value = (b"pdf", "application/pdf")
        extract_text.return_value = "지역제한 없음\n업종제한 없음"

        self.assertEqual(self._queue_for_document_analysis(target_notice, other_notice), 2)
        first = run_pending_bid_notice_document_analysis(
            self.db,
            now=self.now,
            notice_ids=[target_notice.id],
        )
        force_bid_notice_document_reanalysis(self.db, notice_id=target_notice.id)
        self.db.commit()
        second = run_pending_bid_notice_document_analysis(
            self.db,
            now=self.now + timedelta(minutes=1),
            notice_ids=[target_notice.id],
        )

        self.assertEqual(first["analyzed_count"], 1)
        self.assertEqual(second["analyzed_count"], 1)
        self.assertEqual(download_attachment.call_count, 2)
        self.assertIsNone(
            self.db.scalar(
                select(BidNoticeDocumentAnalysisModel).where(
                    BidNoticeDocumentAnalysisModel.notice_id == other_notice.id
                )
            )
        )

    @patch("app.g2b.bid_notices.document_analysis._download_attachment")
    @patch("app.g2b.bid_notices.document_analysis.fetch_explicit_region_restriction")
    @patch("app.g2b.bid_notices.document_analysis.fetch_industry_restriction_codes")
    @patch("app.g2b.bid_notices.document_analysis.fetch_participant_region_restriction")
    def test_network_failure_is_retried_after_lease_delay(
        self,
        fetch_region,
        fetch_industry,
        fetch_explicit_region,
        download_attachment,
    ):
        notice = self._add_matched_notice()
        fetch_region.return_value = (None, REGION_API_EMPTY)
        fetch_explicit_region.return_value = (None, None)
        fetch_industry.return_value = (None, INDUSTRY_API_EMPTY)
        download_attachment.side_effect = AttachmentDownloadError(
            "DOWNLOAD_NETWORK", retryable=True
        )

        self.assertEqual(self._queue_for_document_analysis(notice), 1)

        first = run_pending_bid_notice_document_analysis(self.db, now=self.now)
        before_retry = run_pending_bid_notice_document_analysis(self.db, now=self.now)
        after_retry = run_pending_bid_notice_document_analysis(
            self.db, now=self.now + timedelta(minutes=6)
        )

        analysis = self.db.scalar(
            select(BidNoticeDocumentAnalysisModel).where(
                BidNoticeDocumentAnalysisModel.notice_id == notice.id
            )
        )
        self.assertEqual(first["failed_count"], 1)
        self.assertEqual(before_retry["claimed_count"], 0)
        self.assertEqual(after_retry["failed_count"], 1)
        self.assertEqual(analysis.status, "FAILED")
        self.assertEqual(analysis.attempt_count, 2)
        self.assertEqual(analysis.error_message, "DOWNLOAD_NETWORK")
        response = _notice_response(self.db.get(ScraperNoticeModel, notice.id), "AI", document_analysis=analysis)
        self.assertEqual(response.document_analysis_reason, "첨부파일 연결 실패 · 재시도 대기")

    @patch("app.g2b.bid_notices.document_analysis._download_attachment")
    @patch("app.g2b.bid_notices.document_analysis.fetch_explicit_region_restriction")
    @patch("app.g2b.bid_notices.document_analysis.fetch_industry_restriction_codes")
    @patch("app.g2b.bid_notices.document_analysis.fetch_participant_region_restriction")
    def test_non_retryable_download_failure_is_not_reclaimed(
        self,
        fetch_region,
        fetch_industry,
        fetch_explicit_region,
        download_attachment,
    ):
        notice = self._add_matched_notice()
        fetch_region.return_value = (None, REGION_API_EMPTY)
        fetch_explicit_region.return_value = (None, None)
        fetch_industry.return_value = (None, INDUSTRY_API_EMPTY)
        download_attachment.side_effect = AttachmentDownloadError(
            "DOWNLOAD_HTTP_404", retryable=False
        )

        self.assertEqual(self._queue_for_document_analysis(notice), 1)
        first = run_pending_bid_notice_document_analysis(self.db, now=self.now)
        later = run_pending_bid_notice_document_analysis(
            self.db, now=self.now + timedelta(hours=2)
        )

        analysis = self.db.scalar(
            select(BidNoticeDocumentAnalysisModel).where(
                BidNoticeDocumentAnalysisModel.notice_id == notice.id
            )
        )
        self.assertEqual(first["failed_count"], 1)
        self.assertEqual(later["claimed_count"], 0)
        self.assertEqual(analysis.attempt_count, 3)
        response = _notice_response(
            self.db.get(ScraperNoticeModel, notice.id), "AI", document_analysis=analysis
        )
        self.assertEqual(response.document_analysis_reason, "첨부파일을 찾을 수 없음")
