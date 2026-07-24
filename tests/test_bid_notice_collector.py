import unittest
from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.data.models import Base, ScraperNoticeModel
from app.g2b.bid_notice import KST
from app.g2b.bid_notices.collector import (
    INDUSTRY_API_EMPTY,
    INDUSTRY_API_VALUE,
    REGION_API_VALUE,
    collect_bid_notices,
    collect_scheduled_bid_notices,
    fetch_notice_detail_source,
    fetch_industry_restriction_codes,
    fetch_participant_region_restriction,
)
from app.g2b.bid_notices.matching import (
    dismiss_user_bid_notice,
    restore_user_bid_notice,
    sync_user_bid_notice_matches,
    update_user_bid_notice_profile,
)
from app.g2b.bid_notices.models import (
    BidNoticeCollectionRunModel,
    BidNoticeSheetExportModel,
    UserBidNoticeMatchModel,
    UserBidNoticeStateModel,
)
from app.g2b.bid_notices.router import fetch_bid_notice_detail, list_bid_notices
from app.g2b.bid_notices.schemas import BidNoticeListItem
from app.g2b.bid_notices.sheet_export import (
    build_bid_notice_sheet_rows,
    claim_bid_notice_sheet_exports,
    complete_bid_notice_sheet_exports,
)
from app.g2b.opening_results.matching import SheetExportConflictError
from app.g2b.opening_results.models import SheetDestinationModel


class BidNoticeCollectorTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_api_response_treats_timezone_less_notice_times_as_kst(self):
        item = BidNoticeListItem.model_validate(
            {
                "id": 1,
                "bid_notice_no": "R26BK01641343",
                "bid_notice_ord": "001",
                "business_name": "국제연수관 옥상 지붕설치 공사",
                "demand_agency_name": None,
                "work_type": "공사",
                "procurement_type": None,
                "official_base_amount": None,
                "business_amount": None,
                "published_at": datetime(2026, 7, 24, 9, 0),
                "deadline_at": datetime(2026, 7, 31, 18, 0),
                "notice_url": None,
                "region_restriction": None,
                "region_restriction_api_status": None,
                "industry_restriction_codes": None,
                "icore_industry_code_match": None,
                "is_two_stage_bid": None,
                "joint_supply_allowed": None,
            }
        )

        self.assertEqual(item.published_at, datetime(2026, 7, 24, 9, 0, tzinfo=KST))
        self.assertEqual(item.deadline_at, datetime(2026, 7, 31, 18, 0, tzinfo=KST))
        serialized = item.model_dump(mode="json")
        self.assertEqual(serialized["published_at"], "2026-07-24T09:00:00+09:00")
        self.assertEqual(serialized["deadline_at"], "2026-07-31T18:00:00+09:00")

    @patch("app.g2b.bid_notices.collector._fetch_operation")
    def test_collection_keeps_business_and_official_base_amount_separate(self, fetch_operation):
        fetch_operation.return_value = [
            {
                "bidNtceNo": "R26BK000001",
                "bidNtceOrd": "000",
                "bidNtceNm": "AI 교육 운영 용역",
                "dminsttNm": "테스트 교육청",
                "presmptPrce": "100000000",
                "VAT": "10000000",
                "bsisAmount": "95000000",
                "bidNtceDt": "202607231000",
                "bidClseDt": "202607301700",
                "prtcptPsblRgnNm": "서울특별시",
                "bidNtceDtlClsfcNm": "기술용역",
                "bidMethdNm": "2단계경쟁",
                "cmmnSpldmdMethdNm": "(전자)분담이행",
            }
        ]

        result = collect_bid_notices(
            self.db,
            start_date=date(2026, 7, 20),
            end_date=date(2026, 7, 23),
            business_types=["SERVICE"],
            keywords=["AI"],
        )

        stored = self.db.scalar(select(ScraperNoticeModel))
        run = self.db.scalar(select(BidNoticeCollectionRunModel))
        self.assertEqual(result["fetched_count"], 1)
        self.assertEqual(result["inserted_count"], 1)
        self.assertEqual(stored.base_amount, Decimal("110000000"))
        self.assertEqual(stored.official_base_amount, Decimal("95000000"))
        self.assertEqual(stored.bid_notice_ord, "000")
        self.assertEqual(stored.work_type, "기술용역")
        self.assertTrue(stored.joint_supply_allowed)
        self.assertEqual(fetch_operation.call_args.kwargs["keyword"], "AI")
        # SQLite strips timezone metadata; the source KST wall-clock value must
        # still remain unchanged after the shared persistence round trip.
        self.assertEqual(stored.published_at.hour, 10)
        self.assertEqual(run.status, "SUCCESS")

    @patch("app.g2b.bid_notices.collector._fetch_operation")
    def test_scheduled_collection_uses_enabled_keywords_and_skips_existing_notices(
        self, fetch_operation
    ):
        now = datetime(2026, 7, 24, 8, 0, tzinfo=timezone.utc)
        fetch_operation.return_value = [
            {
                "bidNtceNo": "R26BK000012",
                "bidNtceOrd": "00",
                "bidNtceNm": "AI 데이터 분석 물품 구매",
                "bidNtceDt": "202607241000",
            }
        ]
        update_user_bid_notice_profile(
            self.db,
            organization_id=1,
            user_id=10,
            enabled=True,
            keywords=["AI"],
            excluded_keywords=[],
        )
        update_user_bid_notice_profile(
            self.db,
            organization_id=1,
            user_id=11,
            enabled=True,
            keywords=["AI", "클라우드"],
            excluded_keywords=[],
        )
        update_user_bid_notice_profile(
            self.db,
            organization_id=1,
            user_id=12,
            enabled=False,
            keywords=["제외"],
            excluded_keywords=[],
        )

        first = collect_scheduled_bid_notices(self.db, now=now)
        second = collect_scheduled_bid_notices(self.db, now=now)

        self.assertEqual(first["inserted_count"], 1)
        self.assertEqual(first["updated_count"], 0)
        self.assertEqual(second["inserted_count"], 0)
        self.assertEqual(second["updated_count"], 0)
        searched_keywords = {call.kwargs["keyword"] for call in fetch_operation.call_args_list}
        self.assertEqual(searched_keywords, {"AI", "클라우드"})
        self.assertTrue(
            all(
                call.kwargs["end_at"] == now.astimezone(call.kwargs["end_at"].tzinfo)
                for call in fetch_operation.call_args_list
            )
        )
        self.assertTrue(
            all(call.kwargs["start_at"].date().isoformat() == "2026-07-11" for call in fetch_operation.call_args_list)
        )

    @patch("app.g2b.bid_notices.collector.requests.get")
    def test_notice_detail_source_uses_operation_matching_work_type(self, request_get):
        request_get.return_value.json.return_value = {
            "response": {
                "header": {"resultCode": "00"},
                "body": {
                    "items": {
                        "item": {
                            "bidNtceNo": "R26BK000013",
                            "bidNtceOrd": "00",
                            "ntceSpecFileNm1": "공고문.hwp",
                            "ntceSpecDocUrl1": "https://www.g2b.go.kr/file/notice-13",
                        }
                    }
                },
            }
        }

        with patch("app.g2b.bid_notices.collector.settings.g2b_award_service_key", "test-key"):
            source = fetch_notice_detail_source(
                notice_no="R26BK000013", notice_ord="00", work_type="용역"
            )

        self.assertEqual(source["ntceSpecFileNm1"], "공고문.hwp")
        self.assertIn("getBidPblancListInfoServc", request_get.call_args.args[0])

    @patch("app.g2b.bid_notices.collector.requests.get")
    def test_participant_region_api_returns_official_region_restriction(self, request_get):
        request_get.return_value.json.return_value = {
            "response": {
                "header": {"resultCode": "00"},
                "body": {
                    "items": {
                        "item": {
                            "bidNtceNo": "R26BK01641343",
                            "bidNtceOrd": "001",
                            "prtcptPsblRgnNm": "충청북도",
                        }
                    }
                },
            }
        }

        with patch("app.g2b.bid_notices.collector.settings.g2b_award_service_key", "test-key"):
            region, status = fetch_participant_region_restriction(
                notice_no="R26BK01641343", notice_ord="001"
            )

        self.assertEqual(region, "충청북도")
        self.assertEqual(status, REGION_API_VALUE)
        self.assertIn("getBidPblancListInfoPrtcptPsblRgn", request_get.call_args.args[0])

    @patch("app.g2b.bid_notices.collector.requests.get")
    def test_license_limit_codes_are_saved_as_four_digit_codes(self, request_get):
        request_get.return_value.json.return_value = {
            "response": {
                "header": {"resultCode": "00"},
                "body": {
                    "items": {
                        "item": [
                            {"lcnsLmtNm": "정보통신공사업/0036"},
                            {"permsnIndstrytyList": "[소프트웨어사업자/1468]"},
                        ]
                    }
                },
            }
        }

        with patch("app.g2b.bid_notices.collector.settings.g2b_award_service_key", "test-key"):
            codes, status = fetch_industry_restriction_codes(
                notice_no="R26BK000001", notice_ord="000"
            )

        self.assertEqual(codes, "0036, 1468")
        self.assertEqual(status, INDUSTRY_API_VALUE)

    @patch("app.g2b.bid_notices.router.fetch_industry_restriction_codes")
    def test_detail_loads_and_persists_industry_restriction_codes(self, fetch_codes):
        now = datetime.now(timezone.utc)
        notice = ScraperNoticeModel(
            dedup_key="bid-notice-detail-test",
            notice_id="R26BK000007",
            title="AI 데이터 구축 용역",
            bid_notice_no="R26BK000007",
            bid_notice_ord="00",
            business_name="AI 데이터 구축 용역",
            joint_supply_allowed=True,
            first_seen_at=now,
            last_seen_at=now,
            published_at=now,
            source_payload="{}",
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
        fetch_codes.return_value = ("0036, 1468", INDUSTRY_API_VALUE)

        response = fetch_bid_notice_detail(
            notice_id=notice.id,
            auth={"organization_id": 1, "user_id": 10},
            db=self.db,
        )

        self.assertEqual(response.industry_restriction_codes, "0036, 1468")
        self.assertTrue(response.joint_supply_allowed)
        self.assertEqual(
            self.db.get(ScraperNoticeModel, notice.id).industry_restriction_codes,
            "0036, 1468",
        )

    @patch("app.g2b.bid_notices.router.fetch_industry_restriction_codes")
    def test_icore_code_filter_keeps_matching_and_no_restriction_notices(self, fetch_codes):
        now = datetime.now(timezone.utc)
        matching_notice = ScraperNoticeModel(
            dedup_key="bid-notice-icore-match",
            notice_id="R26BK000008",
            title="AI 아이코어 코드 일치 공고",
            first_seen_at=now,
            last_seen_at=now,
            published_at=now,
            source_payload="{}",
        )
        other_notice = ScraperNoticeModel(
            dedup_key="bid-notice-icore-miss",
            notice_id="R26BK000009",
            title="AI 아이코어 코드 불일치 공고",
            first_seen_at=now,
            last_seen_at=now,
            published_at=now,
            source_payload="{}",
        )
        no_code_notice = ScraperNoticeModel(
            dedup_key="bid-notice-icore-empty",
            notice_id="R26BK000014",
            title="AI 업종제한 해당없음 공고",
            first_seen_at=now,
            last_seen_at=now,
            published_at=now,
            source_payload="{}",
        )
        self.db.add_all([matching_notice, other_notice, no_code_notice])
        self.db.commit()
        update_user_bid_notice_profile(
            self.db,
            organization_id=1,
            user_id=10,
            enabled=True,
            keywords=["AI"],
            excluded_keywords=[],
        )
        fetch_codes.side_effect = lambda *, notice_no, notice_ord: {
            "R26BK000008": ("0036", INDUSTRY_API_VALUE),
            "R26BK000009": ("7777", INDUSTRY_API_VALUE),
            "R26BK000014": (None, INDUSTRY_API_EMPTY),
        }[notice_no]

        response = list_bid_notices(
            q=None,
            work_type=None,
            region=None,
            icore_codes_only=True,
            page=1,
            page_size=30,
            auth={"organization_id": 1, "user_id": 10},
            db=self.db,
        )

        self.assertEqual(response.total, 2)
        self.assertEqual({item.id for item in response.items}, {matching_notice.id, no_code_notice.id})

    def test_review_list_filters_multiple_work_types(self):
        now = datetime.now(timezone.utc)
        notices = [
            ScraperNoticeModel(
                dedup_key=f"bid-notice-work-type-{work_type}",
                notice_id=f"R26BK00002{index}",
                title=f"AI {work_type} 공고",
                work_type=work_type,
                first_seen_at=now,
                last_seen_at=now,
                published_at=now,
                source_payload="{}",
            )
            for index, work_type in enumerate(["공사", "물품", "일반용역", "용역"])
        ]
        self.db.add_all(notices)
        self.db.commit()
        update_user_bid_notice_profile(
            self.db,
            organization_id=1,
            user_id=10,
            enabled=True,
            keywords=["AI"],
            excluded_keywords=[],
        )

        response = list_bid_notices(
            q=None,
            work_type="공사,물품",
            region=None,
            icore_codes_only=False,
            page=1,
            page_size=30,
            auth={"organization_id": 1, "user_id": 10},
            db=self.db,
        )

        self.assertEqual({item.work_type for item in response.items}, {"공사", "물품"})

        general_response = list_bid_notices(
            q=None,
            work_type="일반용역",
            region=None,
            icore_codes_only=False,
            page=1,
            page_size=30,
            auth={"organization_id": 1, "user_id": 10},
            db=self.db,
        )
        self.assertEqual({item.work_type for item in general_response.items}, {"일반용역", "용역"})

    @patch("app.g2b.bid_notices.router.fetch_industry_restriction_codes")
    @patch("app.g2b.bid_notices.router.fetch_notice_detail_source")
    def test_detail_fetches_and_persists_official_notice_attachments(self, fetch_detail, fetch_codes):
        now = datetime.now(timezone.utc)
        notice = ScraperNoticeModel(
            dedup_key="bid-notice-detail-attachments",
            notice_id="R26BK000015",
            bid_notice_no="R26BK000015",
            bid_notice_ord="00",
            title="AI 상세 첨부파일 공고",
            work_type="용역",
            first_seen_at=now,
            last_seen_at=now,
            published_at=now,
            source_payload="{}",
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
        fetch_detail.return_value = {
            "bidNtceNo": "R26BK000015",
            "bidNtceOrd": "00",
            "ntceSpecFileNm1": "제안요청서.pdf",
            "ntceSpecDocUrl1": "https://www.g2b.go.kr/file/notice-15",
        }
        fetch_codes.return_value = (None, INDUSTRY_API_EMPTY)

        response = fetch_bid_notice_detail(
            notice_id=notice.id,
            auth={"organization_id": 1, "user_id": 10},
            db=self.db,
        )

        self.assertEqual(response.attachments[0].label, "제안요청서.pdf")
        self.assertEqual(self.db.get(ScraperNoticeModel, notice.id).source_payload.count("notice-15"), 1)

    @patch("app.g2b.bid_notices.router.fetch_industry_restriction_codes")
    @patch("app.g2b.bid_notices.router.fetch_notice_detail_source")
    def test_detail_preserves_explicit_no_region_from_official_api(self, fetch_detail, fetch_codes):
        now = datetime.now(timezone.utc)
        notice = ScraperNoticeModel(
            dedup_key="bid-notice-detail-no-region",
            notice_id="R26BK000016",
            bid_notice_no="R26BK000016",
            bid_notice_ord="00",
            title="AI 지역제한 없음 공고",
            work_type="용역",
            first_seen_at=now,
            last_seen_at=now,
            published_at=now,
            source_payload="{}",
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
        fetch_detail.return_value = {
            "bidNtceNo": "R26BK000016",
            "bidNtceOrd": "00",
            "prtcptPsblRgnNm": "해당없음",
        }
        fetch_codes.return_value = (None, INDUSTRY_API_EMPTY)

        response = fetch_bid_notice_detail(
            notice_id=notice.id,
            auth={"organization_id": 1, "user_id": 10},
            db=self.db,
        )

        stored = self.db.get(ScraperNoticeModel, notice.id)
        self.assertEqual(response.region_restriction, "해당없음")
        self.assertEqual(stored.region_restriction_api_status, REGION_API_VALUE)

    @patch("app.g2b.bid_notices.router.fetch_participant_region_restriction")
    def test_detail_fetches_missing_participant_region_restriction(self, fetch_region):
        now = datetime.now(timezone.utc)
        notice = ScraperNoticeModel(
            dedup_key="bid-notice-detail-region",
            notice_id="R26BK01641343",
            bid_notice_no="R26BK01641343",
            bid_notice_ord="001",
            title="국제연수관 옥상 지붕설치 공사",
            work_type="공사",
            first_seen_at=now,
            last_seen_at=now,
            published_at=now,
            source_payload='{"_attachments_checked": true}',
        )
        self.db.add(notice)
        self.db.commit()
        update_user_bid_notice_profile(
            self.db,
            organization_id=1,
            user_id=10,
            enabled=True,
            keywords=["국제연수관"],
            excluded_keywords=[],
        )
        sync_user_bid_notice_matches(self.db, organization_id=1, user_id=10, now=now)
        self.db.commit()
        fetch_region.return_value = ("충청북도", REGION_API_VALUE)

        response = fetch_bid_notice_detail(
            notice_id=notice.id,
            auth={"organization_id": 1, "user_id": 10},
            db=self.db,
        )

        self.assertEqual(response.region_restriction, "충청북도")
        self.assertEqual(self.db.get(ScraperNoticeModel, notice.id).region_restriction, "충청북도")
        fetch_region.assert_called_once_with(notice_no="R26BK01641343", notice_ord="001")

    def test_detail_exposes_official_notice_attachments(self):
        now = datetime.now(timezone.utc)
        notice = ScraperNoticeModel(
            dedup_key="bid-notice-attachments",
            notice_id="R26BK000010",
            title="AI 첨부파일 공고",
            first_seen_at=now,
            last_seen_at=now,
            published_at=now,
            source_payload=(
                '{"ntceSpecFileNm1":"공고문.hwp",'
                '"ntceSpecDocUrl1":"https://www.g2b.go.kr/file/notice-1"}'
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

        response = fetch_bid_notice_detail(
            notice_id=notice.id,
            auth={"organization_id": 1, "user_id": 10},
            db=self.db,
        )

        self.assertEqual(response.attachments[0].label, "공고문.hwp")
        self.assertEqual(response.attachments[0].url, "https://www.g2b.go.kr/file/notice-1")

    def test_review_list_searches_notice_number_and_demand_agency(self):
        now = datetime.now(timezone.utc)
        notice = ScraperNoticeModel(
            dedup_key="bid-notice-search-fields",
            notice_id="R26BK000011",
            title="AI 데이터 분석 물품 구매",
            bid_notice_no="R26BK000011",
            demand_agency_name="아이코어교육청",
            first_seen_at=now,
            last_seen_at=now,
            published_at=now,
            source_payload="{}",
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

        response = list_bid_notices(
            q="아이코어교육청",
            work_type=None,
            region=None,
            page=1,
            page_size=30,
            auth={"organization_id": 1, "user_id": 10},
            db=self.db,
        )

        self.assertEqual(response.total, 1)
        self.assertEqual(response.items[0].bid_notice_no, "R26BK000011")

    def test_personal_profile_matches_only_its_owner_and_exclusion_wins(self):
        now = datetime.now(timezone.utc)
        notice = ScraperNoticeModel(
            dedup_key="bid-notice-test",
            notice_id="R26BK000002",
            title="AI 교육 운영 용역",
            bid_notice_no="R26BK000002",
            bid_notice_ord="00",
            business_name="AI 교육 운영 용역",
            first_seen_at=now,
            last_seen_at=now,
            published_at=now,
            source_payload="{}",
        )
        self.db.add(notice)
        self.db.commit()

        update_user_bid_notice_profile(
            self.db,
            organization_id=1,
            user_id=10,
            enabled=True,
            keywords=["AI"],
            excluded_keywords=["운영"],
        )
        update_user_bid_notice_profile(
            self.db,
            organization_id=1,
            user_id=11,
            enabled=True,
            keywords=["AI"],
            excluded_keywords=[],
        )
        sync_user_bid_notice_matches(self.db, organization_id=1, user_id=10, now=now)
        sync_user_bid_notice_matches(self.db, organization_id=1, user_id=11, now=now)
        self.db.commit()

        matches = self.db.scalars(select(UserBidNoticeMatchModel)).all()
        self.assertEqual([(item.user_id, item.notice_id) for item in matches], [(11, notice.id)])

    def test_personal_sheet_export_claim_uses_notice_rows_and_owner_lock(self):
        now = datetime.now(timezone.utc)
        notice = ScraperNoticeModel(
            dedup_key="bid-notice-sheet-test",
            notice_id="R26BK000003",
            title="AI 데이터 구축 용역",
            bid_notice_no="R26BK000003",
            bid_notice_ord="01",
            business_name="AI 데이터 구축 용역",
            base_amount=Decimal("1100000"),
            official_base_amount=Decimal("1000000"),
            first_seen_at=now,
            last_seen_at=now,
            published_at=now,
            source_payload="{}",
        )
        destination = SheetDestinationModel(
            organization_id=1,
            owner_user_id=10,
            label="내 입찰공고",
            spreadsheet_id="sheet-id",
            tab_name="입찰공고",
        )
        self.db.add_all([notice, destination])
        self.db.commit()

        rows = build_bid_notice_sheet_rows([notice])
        self.assertEqual(rows[0][0:2], ["R26BK000003", "01"])
        self.assertEqual(rows[0][8:10], [1100000.0, 1000000.0])
        with self.assertRaises(SheetExportConflictError):
            claim_bid_notice_sheet_exports(
                self.db,
                destination=destination,
                organization_id=1,
                user_id=11,
                notices=[notice],
            )

        claim = claim_bid_notice_sheet_exports(
            self.db,
            destination=destination,
            organization_id=1,
            user_id=10,
            notices=[notice],
        )
        complete_bid_notice_sheet_exports(self.db, claim=claim)
        history = self.db.scalar(select(BidNoticeSheetExportModel))
        self.assertEqual(history.status, "SUCCEEDED")

    def test_dismissed_notice_is_kept_only_in_the_personal_archive_state(self):
        now = datetime.now(timezone.utc)
        notice = ScraperNoticeModel(
            dedup_key="bid-notice-archive-test",
            notice_id="R26BK000004",
            title="AI 플랫폼 용역",
            first_seen_at=now,
            last_seen_at=now,
            published_at=now,
            source_payload="{}",
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
        dismiss_user_bid_notice(
            self.db,
            organization_id=1,
            user_id=10,
            notice_id=notice.id,
        )
        state = self.db.scalar(select(UserBidNoticeStateModel))
        self.assertEqual(state.state, "DISMISSED")
        self.assertTrue(
            restore_user_bid_notice(
                self.db,
                organization_id=1,
                user_id=10,
                notice_id=notice.id,
            )
        )
        self.assertIsNone(self.db.scalar(select(UserBidNoticeStateModel)))

    def test_review_list_filters_by_region(self):
        now = datetime.now(timezone.utc)
        self.db.add_all([
            ScraperNoticeModel(
                dedup_key="bid-notice-region-seoul",
                notice_id="R26BK000005",
                title="AI 서울 교육 용역",
                region_restriction="서울특별시",
                first_seen_at=now,
                last_seen_at=now,
                published_at=now,
                source_payload="{}",
            ),
            ScraperNoticeModel(
                dedup_key="bid-notice-region-busan",
                notice_id="R26BK000006",
                title="AI 부산 교육 용역",
                region_restriction="부산광역시",
                first_seen_at=now,
                last_seen_at=now,
                published_at=now,
                source_payload="{}",
            ),
        ])
        self.db.commit()
        update_user_bid_notice_profile(
            self.db,
            organization_id=1,
            user_id=10,
            enabled=True,
            keywords=["AI"],
            excluded_keywords=[],
        )

        response = list_bid_notices(
            q=None,
            work_type=None,
            region="서울",
            page=1,
            page_size=30,
            auth={"organization_id": 1, "user_id": 10},
            db=self.db,
        )

        self.assertEqual(response.total, 1)
        self.assertEqual(response.items[0].business_name, "AI 서울 교육 용역")
