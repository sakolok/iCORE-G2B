import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import Mock, patch

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.data.models import Base, ScraperNoticeModel, ScraperRunModel
from app.g2b.bid_notice import (
    REGION_API_EMPTY,
    REGION_API_ERROR,
    REGION_API_ORDER_MISMATCH,
    REGION_API_VALUE,
    canonical_bid_notice_identity,
    infer_two_stage_bid,
    missing_bid_notice_context_fields,
    parse_business_amount,
)
from app.g2b.opening_results.models import (
    BidOpeningEntryModel,
    BidOpeningRoundModel,
)
from app.g2b.opening_results.notice_context_repository import canonical_notice_key
from app.g2b.opening_results.sheet_export import (
    build_sheet_rows,
    organize_entry_rankings,
)
from app.schemas import ScraperDedupFilterRequest, ScraperNotice
from app.g2b.bid_notices.service import (
    _fetch_official_bid_notice_context,
    _make_dedup_key,
    enrich_bid_notice_contexts_for_opening_rounds,
    filter_new_scraper_notices,
)
from cloudrun.g2b_worker.main import (
    NoticeRow,
    ScraperJobPayload,
    _build_query_window,
    _enrich_bid_notice_contexts,
    _extract_from_item,
    _fetch_g2b_notices,
    run_scraper,
)


class FakeG2BResponse:
    status_code = 200
    headers = {"content-type": "application/json"}

    def __init__(self, items):
        self.items = items
        self.text = "response"

    def raise_for_status(self):
        return None

    def json(self):
        return {
            "response": {
                "header": {"resultCode": "00", "resultMsg": "OK"},
                "body": {
                    "totalCount": len(self.items),
                    "items": {"item": self.items},
                },
            }
        }


class ScraperNoticePersistenceTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def add_opening_result(self, *, bid_notice_ord: str = "00") -> int:
        now = datetime.now(timezone.utc)
        round_row = BidOpeningRoundModel(
            external_key=f"R26BK00000001|{bid_notice_ord}|0|0",
            business_type="SERVICE",
            bid_notice_no="R26BK00000001",
            bid_notice_ord=bid_notice_ord,
            bid_class_no="0",
            rebid_no="0",
            title="AI 교육 운영 용역",
            status="COMPLETED",
            entries_collected_at=now,
            collected_at=now,
        )
        self.db.add(round_row)
        self.db.flush()
        self.db.add(
            BidOpeningEntryModel(
                round_id=round_row.id,
                external_key=f"entry-{bid_notice_ord}",
                rank=1,
                company_name="일등기업",
                bid_price_score=Decimal("19.5"),
                technical_score=Decimal("75"),
                total_score=Decimal("94.5"),
            )
        )
        self.db.commit()
        return round_row.id

    def persist(self, *notices: ScraperNotice):
        return filter_new_scraper_notices(
            self.db,
            ScraperDedupFilterRequest(
                run_id="notice-contract-test",
                notices=list(notices),
            ),
        )

    def test_official_g2b_fields_persist_and_join_to_sheet_row(self):
        parsed = _extract_from_item(
            {
                "bidNtceNo": " R26BK00000001 ",
                "bidNtceOrd": "000",
                "bidNtceNm": "AI 교육 운영 용역",
                "dminsttNm": "OO대학교",
                "presmptPrce": "150,000,000",
                "VAT": "15,000,000",
                "bssamt": "150,000,000",
                "prearngPrceDcsnMthdNm": "복수예가",
                "bidNtceDt": "202607161030",
                "bidClseDt": "202607201500",
                "prtcptPsblRgnNm": "서울특별시",
                "bidMethdNm": "2단계경쟁",
            }
        )
        self.assertIsNotNone(parsed)
        notice = ScraperNotice.model_validate(
            parsed.model_dump(exclude={"matched_keyword"})
        )

        response = self.persist(notice)
        result_id = self.add_opening_result()
        stored = self.db.scalar(select(ScraperNoticeModel))
        rows, missing_context_keys, missing_result_ids = build_sheet_rows(
            self.db,
            [result_id],
        )

        self.assertEqual(response.kept_count, 1)
        self.assertEqual(stored.bid_notice_no, "R26BK00000001")
        self.assertEqual(stored.bid_notice_ord, "000")
        self.assertEqual(stored.business_name, "AI 교육 운영 용역")
        self.assertEqual(stored.demand_agency_name, "OO대학교")
        self.assertEqual(stored.base_amount, Decimal("165000000.00"))
        self.assertEqual(stored.prearranged_price_decision_method, "복수예가")
        self.assertEqual(stored.region_restriction, "서울특별시")
        self.assertEqual(
            stored.region_restriction_api_status,
            REGION_API_VALUE,
        )
        self.assertTrue(stored.is_two_stage_bid)
        self.assertEqual(notice.published_at.utcoffset(), timedelta(hours=9))
        self.assertEqual(notice.published_at.hour, 10)
        self.assertEqual(missing_context_keys, [])
        self.assertEqual(missing_result_ids, [])
        self.assertEqual(
            rows[0][:9],
            [
                "R26BK00000001",
                "AI 교육 운영 용역",
                "OO대학교",
                165000000,
                "2026-07-20 15:00",
                "서울특별시",
                "Y",
                "일등기업",
                "19.5+75=94.50",
            ],
        )

    def test_missing_official_fields_do_not_fall_back_to_legacy_values(self):
        self.persist(
            ScraperNotice(
                notice_id="R26BK00000001",
                title="AI 교육 운영 용역",
                agency="OO대학교",
                estimated_price="165000000",
                deadline_at=datetime(2026, 7, 20, 15, 0),
                bid_notice_no="R26BK00000001",
                bid_notice_ord="00",
                business_name="AI 교육 운영 용역",
                demand_agency_name="OO대학교",
                region_restriction="없음",
                is_two_stage_bid=False,
            )
        )
        result_id = self.add_opening_result()
        stored = self.db.scalar(select(ScraperNoticeModel))
        _, missing_context_keys, _ = build_sheet_rows(self.db, [result_id])

        self.assertIsNone(stored.base_amount)
        self.assertIsNone(stored.proposal_deadline)
        self.assertEqual(missing_context_keys, ["R26BK00000001|00"])

    def test_opening_result_enrichment_exports_business_amount(self):
        notice = ScraperNotice(
            notice_id="R26BK00000001",
            title="비예가 AI 구축 용역",
            estimated_price="81818182",
            bid_notice_no="R26BK00000001",
            bid_notice_ord="00",
            business_name="비예가 AI 구축 용역",
            demand_agency_name="OO기관",
            base_amount=Decimal("90000000"),
            prearranged_price_decision_method="비예가",
            proposal_deadline=datetime(2026, 7, 20, 15, 0),
            region_restriction="서울특별시",
            region_restriction_api_status=REGION_API_VALUE,
            is_two_stage_bid=False,
        )
        result_id = self.add_opening_result()
        round_row = self.db.get(BidOpeningRoundModel, result_id)

        enriched_count = enrich_bid_notice_contexts_for_opening_rounds(
            self.db,
            [round_row],
            fetch_context=lambda _: notice,
        )
        self.db.commit()

        rows, missing_context_keys, missing_result_ids = build_sheet_rows(
            self.db,
            [result_id],
        )
        stored = self.db.scalar(select(ScraperNoticeModel))

        self.assertEqual(enriched_count, 1)
        self.assertEqual(stored.estimated_price, "81818182")
        self.assertEqual(stored.base_amount, Decimal("90000000.00"))
        self.assertEqual(stored.prearranged_price_decision_method, "비예가")
        self.assertNotIn(
            "base_amount",
            missing_bid_notice_context_fields(notice),
        )
        self.assertEqual(missing_context_keys, [])
        self.assertEqual(missing_result_ids, [])
        self.assertEqual(rows[0][3], 90000000)

    def test_complete_opening_result_context_is_not_fetched_again(self):
        notice = ScraperNotice(
            notice_id="R26BK00000001",
            title="AI 구축 용역",
            estimated_price="150000000",
            bid_notice_no="R26BK00000001",
            bid_notice_ord="00",
            business_name="AI 구축 용역",
            demand_agency_name="OO기관",
            base_amount=Decimal("165000000"),
            prearranged_price_decision_method="비예가",
            proposal_deadline=datetime(2026, 7, 20, 15, 0),
            region_restriction="서울특별시",
            region_restriction_api_status=REGION_API_VALUE,
            is_two_stage_bid=False,
        )
        result_id = self.add_opening_result()
        round_row = self.db.get(BidOpeningRoundModel, result_id)
        first_fetch = Mock(return_value=notice)
        second_fetch = Mock(return_value=notice)

        self.assertEqual(
            enrich_bid_notice_contexts_for_opening_rounds(
                self.db,
                [round_row],
                fetch_context=first_fetch,
            ),
            1,
        )
        self.db.commit()
        self.assertEqual(
            enrich_bid_notice_contexts_for_opening_rounds(
                self.db,
                [round_row],
                fetch_context=second_fetch,
            ),
            0,
        )

        first_fetch.assert_called_once_with(round_row)
        second_fetch.assert_not_called()

    def test_business_amount_is_estimated_price_plus_vat(self):
        self.assertEqual(
            parse_business_amount(
                {
                    "presmptPrce": "81,818,182",
                    "VAT": "8,181,818",
                    "asignBdgtAmt": "90,000,000",
                    "bssamt": "999",
                }
            ),
            Decimal("90000000"),
        )
        self.assertIsNone(
            parse_business_amount(
                {
                    "asignBdgtAmt": "90,000,000",
                    "bssamt": "90,000,000",
                }
            )
        )

    def test_official_context_truncates_long_region_restriction(self):
        result_id = self.add_opening_result()
        round_row = self.db.get(BidOpeningRoundModel, result_id)
        notice_item = {
            "bidNtceNo": "R26BK00000001",
            "bidNtceOrd": "00",
            "bidNtceNm": "AI 구축 용역",
            "dminsttNm": "OO기관",
            "presmptPrce": "81818182",
            "VAT": "8181818",
            "bidClseDt": "202607201500",
            "prtcptPsblRgnNm": "메인 응답 지역",
        }
        region_items = [
            {
                "bidNtceNo": "R26BK00000001",
                "bidNtceOrd": "00",
                "prtcptPsblRgnNm": f"지역제한-{index:02d}",
            }
            for index in range(40)
        ]

        with patch(
            "app.g2b.bid_notices.service._fetch_bid_notice_api_items",
            side_effect=[(True, [notice_item]), (True, region_items)],
        ):
            notice = _fetch_official_bid_notice_context(round_row)

        self.assertIsNotNone(notice)
        self.assertEqual(notice.base_amount, Decimal("90000000"))
        self.assertEqual(len(notice.region_restriction), 240)
        self.assertTrue(notice.region_restriction.startswith("지역제한-00"))
        self.assertEqual(
            notice.region_restriction_api_status,
            REGION_API_VALUE,
        )

    def test_official_context_marks_empty_region_api_response(self):
        result_id = self.add_opening_result()
        round_row = self.db.get(BidOpeningRoundModel, result_id)
        notice_item = {
            "bidNtceNo": "R26BK00000001",
            "bidNtceOrd": "00",
            "bidNtceNm": "AI 구축 용역",
            "dminsttNm": "OO기관",
            "presmptPrce": "81818182",
            "VAT": "8181818",
            "bidClseDt": "202607201500",
        }

        with patch(
            "app.g2b.bid_notices.service._fetch_bid_notice_api_items",
            side_effect=[(True, [notice_item]), (True, [])],
        ):
            notice = _fetch_official_bid_notice_context(round_row)

        self.assertIsNotNone(notice)
        self.assertIsNone(notice.region_restriction)
        self.assertEqual(
            notice.region_restriction_api_status,
            REGION_API_EMPTY,
        )

    def test_official_context_marks_region_api_error(self):
        result_id = self.add_opening_result()
        round_row = self.db.get(BidOpeningRoundModel, result_id)
        notice_item = {
            "bidNtceNo": "R26BK00000001",
            "bidNtceOrd": "00",
            "bidNtceNm": "AI 구축 용역",
            "dminsttNm": "OO기관",
            "presmptPrce": "81818182",
            "VAT": "8181818",
            "bidClseDt": "202607201500",
        }

        with patch(
            "app.g2b.bid_notices.service._fetch_bid_notice_api_items",
            side_effect=[(True, [notice_item]), (False, [])],
        ):
            notice = _fetch_official_bid_notice_context(round_row)

        self.assertIsNotNone(notice)
        self.assertIsNone(notice.region_restriction)
        self.assertEqual(
            notice.region_restriction_api_status,
            REGION_API_ERROR,
        )

    def test_official_context_marks_mismatched_region_response_for_review(self):
        result_id = self.add_opening_result()
        round_row = self.db.get(BidOpeningRoundModel, result_id)
        notice_item = {
            "bidNtceNo": "R26BK00000001",
            "bidNtceOrd": "00",
            "bidNtceNm": "AI 구축 용역",
        }
        mismatched_region_item = {
            "bidNtceNo": "R26BK99999999",
            "bidNtceOrd": "00",
            "prtcptPsblRgnNm": "서울특별시",
        }

        with patch(
            "app.g2b.bid_notices.service._fetch_bid_notice_api_items",
            side_effect=[
                (True, [notice_item]),
                (True, [mismatched_region_item]),
            ],
        ):
            notice = _fetch_official_bid_notice_context(round_row)

        self.assertIsNotNone(notice)
        self.assertIsNone(notice.region_restriction)
        self.assertEqual(
            notice.region_restriction_api_status,
            REGION_API_ORDER_MISMATCH,
        )

    def test_empty_and_error_region_api_outcomes_persist_separately(self):
        worker_notices = [
            NoticeRow(
                notice_id="R26BK00000001",
                title="빈 지역 응답 용역",
                bid_notice_no="R26BK00000001",
                bid_notice_ord="00",
                region_restriction_api_status=REGION_API_EMPTY,
            ),
            NoticeRow(
                notice_id="R26BK00000002",
                title="지역 API 오류 용역",
                bid_notice_no="R26BK00000002",
                bid_notice_ord="00",
                region_restriction_api_status=REGION_API_ERROR,
            ),
        ]
        self.persist(
            *[
                ScraperNotice.model_validate(
                    notice.model_dump(exclude={"matched_keyword"})
                )
                for notice in worker_notices
            ]
        )

        stored = {
            row.bid_notice_no: row
            for row in self.db.scalars(select(ScraperNoticeModel)).all()
        }
        self.assertIsNone(stored["R26BK00000001"].region_restriction)
        self.assertEqual(
            stored["R26BK00000001"].region_restriction_api_status,
            REGION_API_EMPTY,
        )
        self.assertIsNone(stored["R26BK00000002"].region_restriction)
        self.assertEqual(
            stored["R26BK00000002"].region_restriction_api_status,
            REGION_API_ERROR,
        )

    def test_region_api_outcomes_keep_stored_context_consistent(self):
        common = {
            "notice_id": "R26BK00000001",
            "title": "AI 교육 운영 용역",
            "bid_notice_no": "R26BK00000001",
            "bid_notice_ord": "00",
        }
        self.persist(
            ScraperNotice(
                **common,
                region_restriction="없음",
            )
        )
        self.persist(
            ScraperNotice(
                **common,
                region_restriction_api_status=REGION_API_EMPTY,
            )
        )
        stored = self.db.scalar(select(ScraperNoticeModel))
        self.assertIsNone(stored.region_restriction)
        self.assertEqual(
            stored.region_restriction_api_status,
            REGION_API_EMPTY,
        )

        self.persist(
            ScraperNotice(
                **common,
                region_restriction="서울특별시",
                region_restriction_api_status=REGION_API_VALUE,
            )
        )
        self.persist(
            ScraperNotice(
                **common,
                region_restriction_api_status=REGION_API_ERROR,
            )
        )
        stored = self.db.scalar(select(ScraperNoticeModel))
        self.assertEqual(stored.region_restriction, "서울특별시")
        self.assertEqual(
            stored.region_restriction_api_status,
            REGION_API_ERROR,
        )
        self.assertIn(
            "region_restriction",
            missing_bid_notice_context_fields(stored),
        )

    def test_missing_official_rank_is_filled_by_total_score(self):
        result_id = self.add_opening_result()
        first = self.db.scalar(
            select(BidOpeningEntryModel).where(
                BidOpeningEntryModel.round_id == result_id
            )
        )
        first.bid_price_score = Decimal("19.9373")
        first.technical_score = Decimal("74.7")
        first.total_score = Decimal("94.6373")
        first.official_total_score = Decimal("94.6373")
        second = BidOpeningEntryModel(
            round_id=result_id,
            external_key="entry-unranked-second",
            rank=None,
            company_name="(주)모노믹스",
            bid_price_score=Decimal("20"),
            technical_score=Decimal("64.3"),
            total_score=Decimal("84.3"),
            official_total_score=Decimal("84.3"),
        )
        self.db.add(second)
        self.db.commit()

        entries = self.db.scalars(
            select(BidOpeningEntryModel)
            .where(BidOpeningEntryModel.round_id == result_id)
            .order_by(BidOpeningEntryModel.id.asc())
        ).all()
        ranked = organize_entry_rankings(entries)
        rows, _, _ = build_sheet_rows(self.db, [result_id])

        self.assertEqual([item.rank for item in ranked], [1, 2])
        self.assertEqual(ranked[0].source, "OFFICIAL")
        self.assertEqual(ranked[1].source, "SCORE_CALCULATED")
        self.assertEqual(ranked[1].entry.company_name, "(주)모노믹스")
        self.assertEqual(
            rows[0][7:11],
            [
                "일등기업",
                "19.9373+74.7=94.64",
                "(주)모노믹스",
                "20+64.3=84.30",
            ],
        )

    def test_missing_base_amount_is_blocked_for_ordinary_price_method(self):
        notice = ScraperNotice(
            notice_id="R26BK00000001",
            title="복수예가 AI 구축 용역",
            bid_notice_no="R26BK00000001",
            bid_notice_ord="00",
            business_name="복수예가 AI 구축 용역",
            demand_agency_name="OO기관",
            prearranged_price_decision_method="복수예가",
            proposal_deadline=datetime(2026, 7, 20, 15, 0),
            region_restriction="없음",
            is_two_stage_bid=False,
        )
        self.persist(notice)
        result_id = self.add_opening_result()

        _, missing_context_keys, _ = build_sheet_rows(self.db, [result_id])

        self.assertIn(
            "base_amount",
            missing_bid_notice_context_fields(notice),
        )
        self.assertEqual(missing_context_keys, ["R26BK00000001|00"])

    def test_missing_base_amount_is_blocked_when_price_method_is_unknown(self):
        notice = ScraperNotice(
            notice_id="R26BK00000001",
            title="가격방식 미확인 AI 구축 용역",
            bid_notice_no="R26BK00000001",
            bid_notice_ord="00",
            business_name="가격방식 미확인 AI 구축 용역",
            demand_agency_name="OO기관",
            proposal_deadline=datetime(2026, 7, 20, 15, 0),
            region_restriction="없음",
            is_two_stage_bid=False,
        )
        self.persist(notice)
        result_id = self.add_opening_result()

        _, missing_context_keys, _ = build_sheet_rows(self.db, [result_id])

        self.assertIn(
            "base_amount",
            missing_bid_notice_context_fields(notice),
        )
        self.assertEqual(missing_context_keys, ["R26BK00000001|00"])

    def test_api_empty_region_requires_review_and_blocks_sheet_export(self):
        notice = ScraperNotice(
            notice_id="R26BK00000001",
            title="AI 구축 용역",
            bid_notice_no="R26BK00000001",
            bid_notice_ord="00",
            business_name="AI 구축 용역",
            demand_agency_name="OO기관",
            base_amount=Decimal("266264460"),
            prearranged_price_decision_method="복수예가",
            proposal_deadline=datetime(2026, 7, 20, 15, 0),
            region_restriction_api_status=REGION_API_EMPTY,
            is_two_stage_bid=False,
        )
        self.persist(notice)
        result_id = self.add_opening_result()

        _, missing_context_keys, _ = build_sheet_rows(self.db, [result_id])

        self.assertIn(
            "region_restriction",
            missing_bid_notice_context_fields(notice),
        )
        self.assertEqual(missing_context_keys, ["R26BK00000001|00"])

    def test_zero_padded_duplicate_notice_orders_create_one_official_row(self):
        self.assertEqual(
            canonical_bid_notice_identity(" R26BK00000001 ", "000"),
            canonical_notice_key("R26BK00000001", "00"),
        )
        common = {
            "title": "AI 교육 운영 용역",
            "bid_notice_no": "R26BK00000001",
            "business_name": "AI 교육 운영 용역",
            "demand_agency_name": "OO대학교",
            "base_amount": Decimal("165000000"),
            "proposal_deadline": datetime(2026, 7, 20, 15, 0),
            "region_restriction": "없음",
            "is_two_stage_bid": False,
        }
        response = self.persist(
            ScraperNotice(**common, notice_id="legacy-00", bid_notice_ord="00"),
            ScraperNotice(**common, notice_id="legacy-000", bid_notice_ord="000"),
        )

        count = self.db.scalar(select(func.count(ScraperNoticeModel.id)))
        self.assertEqual(response.input_count, 2)
        self.assertEqual(response.kept_count, 1)
        self.assertEqual(response.filtered_count, 1)
        self.assertEqual(count, 1)

    def test_worker_preserves_business_amount_and_enriches_region(self):
        notice = NoticeRow(
            notice_id="R26BK00000001",
            title="AI 교육 운영 용역",
            bid_notice_no="R26BK00000001",
            bid_notice_ord="00",
            business_name="AI 교육 운영 용역",
            demand_agency_name="OO대학교",
            base_amount=Decimal("165000000"),
            proposal_deadline=datetime(2026, 7, 20, 15, 0),
            is_two_stage_bid=False,
        )
        region_response = FakeG2BResponse(
            [
                {
                    "bidNtceNo": "R26BK00000001",
                    "bidNtceOrd": "00",
                    "prtcptPsblRgnNm": "서울특별시",
                },
                {
                    "bidNtceNo": "R26BK00000001",
                    "bidNtceOrd": "00",
                    "prtcptPsblRgnNm": "경기도",
                },
            ]
        )

        with patch.dict(
            "os.environ",
            {
                "G2B_SOURCE_URL": (
                    "https://apis.data.go.kr/1230000/ad/BidPublicInfoService/"
                    "getBidPblancListInfoServc"
                ),
                "G2B_SERVICE_KEY": "test-key",
            },
            clear=False,
        ), patch(
            "cloudrun.g2b_worker.main._fetch_g2b_rows",
            return_value=[notice],
        ), patch(
            "cloudrun.g2b_worker.main.requests.get",
            return_value=region_response,
        ) as get_mock:
            rows = _fetch_g2b_notices(["AI"])

        self.assertEqual(rows[0].base_amount, Decimal("165000000"))
        self.assertEqual(rows[0].region_restriction, "서울특별시, 경기도")
        self.assertEqual(
            rows[0].region_restriction_api_status,
            REGION_API_VALUE,
        )
        self.assertEqual(get_mock.call_count, 1)
        self.assertTrue(
            get_mock.call_args.args[0].endswith(
                "/getBidPblancListInfoPrtcptPsblRgn"
            )
        )
        self.assertEqual(
            get_mock.call_args.kwargs["params"]["inqryDiv"],
            "2",
        )

    def test_worker_keeps_business_amount_for_nonpriced_notice(self):
        notice = NoticeRow(
            notice_id="R26BK00000001",
            title="비예가 AI 교육 운영 용역",
            bid_notice_no="R26BK00000001",
            bid_notice_ord="00",
            business_name="비예가 AI 교육 운영 용역",
            demand_agency_name="OO대학교",
            base_amount=Decimal("90000000"),
            prearranged_price_decision_method="비예가",
            proposal_deadline=datetime(2026, 7, 20, 15, 0),
            is_two_stage_bid=False,
        )
        region_response = FakeG2BResponse(
            [
                {
                    "bidNtceNo": "R26BK00000001",
                    "bidNtceOrd": "000",
                    "prtcptPsblRgnNm": "서울특별시",
                }
            ]
        )

        with patch.dict(
            "os.environ",
            {
                "G2B_SOURCE_URL": (
                    "https://apis.data.go.kr/1230000/ad/BidPublicInfoService/"
                    "getBidPblancListInfoServc"
                ),
                "G2B_SERVICE_KEY": "test-key",
            },
            clear=False,
        ), patch(
            "cloudrun.g2b_worker.main._fetch_g2b_rows",
            return_value=[notice],
        ), patch(
            "cloudrun.g2b_worker.main.requests.get",
            return_value=region_response,
        ) as get_mock:
            rows = _fetch_g2b_notices(["AI"])

        self.assertEqual(rows[0].base_amount, Decimal("90000000"))
        self.assertEqual(rows[0].prearranged_price_decision_method, "비예가")
        self.assertEqual(rows[0].region_restriction, "서울특별시")
        self.assertEqual(missing_bid_notice_context_fields(rows[0]), [])
        self.assertEqual(get_mock.call_count, 1)
        self.assertTrue(
            get_mock.call_args.args[0].endswith(
                "/getBidPblancListInfoPrtcptPsblRgn"
            )
        )

    def test_worker_marks_empty_region_api_response(self):
        notice = NoticeRow(
            notice_id="R26BK00000001",
            title="AI 교육 운영 용역",
            bid_notice_no="R26BK00000001",
            bid_notice_ord="00",
        )

        with patch(
            "cloudrun.g2b_worker.main._fetch_bid_notice_detail_items",
            return_value=(True, []),
        ):
            rows = _enrich_bid_notice_contexts([notice])

        self.assertIsNone(rows[0].region_restriction)
        self.assertEqual(
            rows[0].region_restriction_api_status,
            REGION_API_EMPTY,
        )

    def test_worker_marks_region_api_error(self):
        notice = NoticeRow(
            notice_id="R26BK00000001",
            title="AI 교육 운영 용역",
            bid_notice_no="R26BK00000001",
            bid_notice_ord="00",
        )

        with patch(
            "cloudrun.g2b_worker.main._fetch_bid_notice_detail_items",
            return_value=(False, []),
        ):
            rows = _enrich_bid_notice_contexts([notice])

        self.assertIsNone(rows[0].region_restriction)
        self.assertEqual(
            rows[0].region_restriction_api_status,
            REGION_API_ERROR,
        )

    def test_worker_marks_mismatched_region_response_for_review(self):
        notice = NoticeRow(
            notice_id="R26BK00000001",
            title="AI 교육 운영 용역",
            bid_notice_no="R26BK00000001",
            bid_notice_ord="00",
        )
        mismatched_item = {
            "bidNtceNo": "R26BK99999999",
            "bidNtceOrd": "00",
            "prtcptPsblRgnNm": "서울특별시",
        }

        with patch(
            "cloudrun.g2b_worker.main._fetch_bid_notice_detail_items",
            return_value=(True, [mismatched_item]),
        ):
            rows = _enrich_bid_notice_contexts([notice])

        self.assertIsNone(rows[0].region_restriction)
        self.assertEqual(
            rows[0].region_restriction_api_status,
            REGION_API_ORDER_MISMATCH,
        )

    def test_missing_refresh_values_do_not_erase_stored_official_context(self):
        published_at = datetime(2026, 7, 16, 10, 30, tzinfo=timezone.utc)
        deadline_at = datetime(2026, 7, 20, 6, 0, tzinfo=timezone.utc)
        complete = ScraperNotice(
            notice_id="R26BK00000001",
            title="AI 교육 운영 용역",
            agency="공고기관",
            estimated_price="150000000",
            published_at=published_at,
            deadline_at=deadline_at,
            notice_url="https://example.test/notice",
            bid_notice_no="R26BK00000001",
            bid_notice_ord="00",
            business_name="AI 교육 운영 용역",
            demand_agency_name="OO대학교",
            base_amount=Decimal("165000000"),
            prearranged_price_decision_method="복수예가",
            proposal_deadline=datetime(2026, 7, 20, 15, 0),
            region_restriction="없음",
            is_two_stage_bid=False,
        )
        self.persist(complete)
        self.persist(
            ScraperNotice(
                notice_id="R26BK00000001",
                title="AI 교육 운영 용역",
                bid_notice_no="R26BK00000001",
                bid_notice_ord="000",
                business_name="AI 교육 운영 용역",
                demand_agency_name="OO대학교",
            )
        )

        stored = self.db.scalar(select(ScraperNoticeModel))
        self.assertEqual(stored.base_amount, Decimal("165000000.00"))
        self.assertEqual(stored.prearranged_price_decision_method, "복수예가")
        self.assertIsNotNone(stored.proposal_deadline)
        self.assertEqual(stored.region_restriction, "없음")
        self.assertFalse(stored.is_two_stage_bid)
        self.assertEqual(stored.agency, "공고기관")
        self.assertEqual(stored.estimated_price, "150000000")
        self.assertEqual(stored.published_at, published_at.replace(tzinfo=None))
        self.assertEqual(stored.deadline_at, deadline_at.replace(tzinfo=None))
        self.assertEqual(stored.notice_url, "https://example.test/notice")

    def test_existing_legacy_notice_is_upgraded_in_place(self):
        now = datetime.now(timezone.utc)
        legacy_notice = ScraperNotice(
            notice_id="R26BK00000001",
            title="AI 교육 운영 용역",
            published_at=now - timedelta(days=2),
        )
        legacy_row = ScraperNoticeModel(
            dedup_key=_make_dedup_key(legacy_notice),
            notice_id=legacy_notice.notice_id,
            title=legacy_notice.title,
            first_seen_at=now,
            last_seen_at=now,
        )
        self.db.add_all(
            [
                legacy_row,
                ScraperRunModel(
                    run_id="previous-success",
                    source="test",
                    status="success",
                    keyword_count=1,
                    notice_count=1,
                    deduped_count=0,
                    email_sent_count=0,
                    sheet_written_count=0,
                    executed_at=now - timedelta(days=1),
                ),
            ]
        )
        self.db.commit()
        legacy_id = legacy_row.id

        response = self.persist(
            ScraperNotice(
                notice_id="R26BK00000001",
                title="AI 교육 운영 용역",
                published_at=now - timedelta(days=2),
                bid_notice_no="R26BK00000001",
                bid_notice_ord="00",
                business_name="AI 교육 운영 용역",
                demand_agency_name="OO대학교",
                base_amount=Decimal("165000000"),
                proposal_deadline=datetime(2026, 7, 20, 15, 0),
                region_restriction="없음",
                is_two_stage_bid=False,
            )
        )

        rows = self.db.scalars(select(ScraperNoticeModel)).all()
        self.assertEqual(response.kept_count, 0)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].id, legacy_id)
        self.assertEqual(rows[0].bid_notice_no, "R26BK00000001")
        self.assertEqual(rows[0].bid_notice_ord, "00")
        self.assertEqual(rows[0].business_name, "AI 교육 운영 용역")
        self.assertEqual(rows[0].demand_agency_name, "OO대학교")
        self.assertEqual(rows[0].base_amount, Decimal("165000000.00"))
        self.assertIsNotNone(rows[0].proposal_deadline)
        self.assertEqual(rows[0].region_restriction, "없음")
        self.assertFalse(rows[0].is_two_stage_bid)

        result_id = self.add_opening_result()
        _, missing_context_keys, missing_result_ids = build_sheet_rows(
            self.db,
            [result_id],
        )
        self.assertEqual(missing_context_keys, ["R26BK00000001|00"])
        self.assertEqual(missing_result_ids, [])

    def test_unseen_stale_notice_is_not_persisted_or_reexposed(self):
        now = datetime.now(timezone.utc)
        self.db.add(
            ScraperRunModel(
                run_id="previous-success",
                source="test",
                status="success",
                keyword_count=1,
                notice_count=1,
                deduped_count=0,
                email_sent_count=0,
                sheet_written_count=0,
                executed_at=now - timedelta(days=1),
            )
        )
        self.db.commit()

        response = self.persist(
            ScraperNotice(
                notice_id="R26BK00000002",
                title="과거 AI 교육 운영 용역",
                published_at=now - timedelta(days=2),
                bid_notice_no="R26BK00000002",
                bid_notice_ord="00",
                business_name="과거 AI 교육 운영 용역",
                demand_agency_name="OO대학교",
                base_amount=Decimal("100000000"),
                proposal_deadline=datetime(2026, 7, 20, 15, 0),
                region_restriction="없음",
                is_two_stage_bid=False,
            )
        )

        stored_count = self.db.scalar(select(func.count(ScraperNoticeModel.id)))
        self.assertEqual(response.kept_count, 0)
        self.assertEqual(response.notices, [])
        self.assertEqual(stored_count, 0)

    def test_two_stage_inference_keeps_unknown_method_unresolved(self):
        self.assertTrue(infer_two_stage_bid(None, "2단계경쟁"))
        self.assertTrue(infer_two_stage_bid(None, "규격·가격 동시입찰"))
        self.assertFalse(infer_two_stage_bid(None, "협상에 의한 계약"))
        self.assertIsNone(infer_two_stage_bid(None, "전자입찰"))

    def test_worker_query_window_is_formatted_in_kst(self):
        fixed_now = datetime(2026, 7, 16, 6, 0, tzinfo=timezone.utc)

        class FixedDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed_now if tz is None else fixed_now.astimezone(tz)

        with (
            patch("cloudrun.g2b_worker.main.datetime", FixedDatetime),
            patch(
                "cloudrun.g2b_worker.main._fetch_last_run_at",
                return_value=datetime(2026, 7, 16, 0, 0, tzinfo=timezone.utc),
            ),
        ):
            self.assertEqual(
                _build_query_window(),
                ("202607160900", "202607161500"),
            )

    def test_worker_marks_incomplete_official_context_as_failed_for_retry(self):
        incomplete = NoticeRow(
            notice_id="R26BK00000001",
            title="AI 교육 운영 용역",
            bid_notice_no="R26BK00000001",
            bid_notice_ord="00",
            business_name="AI 교육 운영 용역",
            demand_agency_name="OO대학교",
            proposal_deadline=datetime(2026, 7, 20, 15, 0),
            is_two_stage_bid=False,
        )
        report_mock = patch("cloudrun.g2b_worker.main._report_run_result")
        with patch(
            "cloudrun.g2b_worker.main._fetch_g2b_notices",
            return_value=[incomplete],
        ), patch(
            "cloudrun.g2b_worker.main._fetch_g2b_prestandards",
            return_value=[],
        ), patch(
            "cloudrun.g2b_worker.main._dedup_with_backend",
            return_value=[incomplete],
        ), patch(
            "cloudrun.g2b_worker.main._append_to_sheet",
            side_effect=[1, 0],
        ), patch(
            "cloudrun.g2b_worker.main._notify_recipients",
            return_value=True,
        ), report_mock as report:
            result = run_scraper(
                ScraperJobPayload(
                    receiver_emails=["admin@example.com"],
                    keywords=["AI"],
                )
            )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(report.call_args.kwargs["status"], "failed")
        self.assertIn("incomplete official context", report.call_args.kwargs["error_message"])


if __name__ == "__main__":
    unittest.main()
