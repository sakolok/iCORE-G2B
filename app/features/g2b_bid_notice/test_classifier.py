import unittest
from datetime import date, datetime

from app.features.g2b_bid_notice.classifier import (
    apply_industry_restriction_exclusion,
    classify_bid_notice,
)
from app.features.g2b_bid_notice.contracts import BidNoticeStorageRecord
from app.features.g2b_bid_notice.schemas import (
    BidNoticeCandidate,
    BidNoticePreviewItem,
    EnrichmentCheck,
    OrganizationGroup,
    PersonalCollectionSettings,
)


def make_settings(**patch):
    defaults = {
        "organization_groups": [
            OrganizationGroup(
                id="education",
                name="교육청·교육지원청",
                parent_agencies=["서울특별시교육청"],
                child_agencies=["서울특별시교육청 AI교육지원센터"],
                agency_codes=["EDU-SEOUL"],
            )
        ],
        "work_types": ["일반용역"],
        "procurement_types": ["내자"],
        "required_title_keywords": ["AI", "교육"],
        "excluded_title_keywords": ["시설"],
    }
    defaults.update(patch)
    return PersonalCollectionSettings(**defaults)


class PersonalCollectionClassifierTests(unittest.TestCase):
    def test_agency_keyword_never_satisfies_title_keyword(self):
        status, decisions = classify_bid_notice(
            make_settings(),
            BidNoticeCandidate(
                bid_ntce_nm="2026 미래교육 프로그램 운영 용역",
                demand_agency_name="서울특별시교육청 AI교육지원센터",
                demand_agency_code="EDU-SEOUL",
                work_type="일반용역",
                procurement_type="내자",
            ),
        )

        self.assertEqual(status, "EXCLUDE")
        title_check = next(decision for decision in decisions if decision.column == "공고명 필수 키워드")
        self.assertEqual(title_check.status, "FAIL")

    def test_missing_active_detail_value_requires_review_not_exclusion(self):
        status, decisions = classify_bid_notice(
            make_settings(base_amount_min=10_000_000),
            BidNoticeCandidate(
                bid_ntce_nm="AI 교육 프로그램 운영 용역",
                demand_agency_name="서울특별시교육청",
                demand_agency_code="EDU-SEOUL",
                work_type="일반용역",
                procurement_type="내자",
                base_amount=None,
            ),
        )

        self.assertEqual(status, "REVIEW")
        amount_check = next(decision for decision in decisions if decision.column == "기초금액")
        self.assertEqual(amount_check.status, "PENDING")

    def test_clear_posted_date_mismatch_is_excluded(self):
        status, _ = classify_bid_notice(
            make_settings(posted_date_start=date(2026, 8, 1)),
            BidNoticeCandidate(
                bid_ntce_nm="AI 교육 프로그램 운영 용역",
                demand_agency_name="서울특별시교육청",
                demand_agency_code="EDU-SEOUL",
                work_type="일반용역",
                procurement_type="내자",
                published_at=datetime(2026, 7, 31, 9, 0),
            ),
        )

        self.assertEqual(status, "EXCLUDE")

    def test_required_and_excluded_keyword_together_requires_review(self):
        status, decisions = classify_bid_notice(
            make_settings(required_title_keywords=["AI"], excluded_title_keywords=["시설"]),
            BidNoticeCandidate(
                bid_ntce_nm="AI 교육시설 운영 용역",
                demand_agency_name="서울특별시교육청",
                demand_agency_code="EDU-SEOUL",
                work_type="일반용역",
                procurement_type="내자",
            ),
        )

        self.assertEqual(status, "REVIEW")
        excluded_check = next(decision for decision in decisions if decision.column == "공고명 제외 키워드")
        self.assertEqual(excluded_check.status, "PENDING")

    def test_excluded_keyword_without_required_keyword_stays_excluded(self):
        status, _ = classify_bid_notice(
            make_settings(required_title_keywords=["AI"], excluded_title_keywords=["시설"]),
            BidNoticeCandidate(
                bid_ntce_nm="시설 유지보수 용역",
                demand_agency_name="서울특별시교육청",
                demand_agency_code="EDU-SEOUL",
                work_type="일반용역",
                procurement_type="내자",
            ),
        )

        self.assertEqual(status, "EXCLUDE")

    def test_industry_restriction_code_mismatch_moves_notice_to_exclude(self):
        item = BidNoticePreviewItem(
            record_id="R26BK0000000-00",
            bid_notice_no="R26BK0000000",
            bid_notice_ord="00",
            detail_enrichment_status="DETAIL_COMPLETED",
            match_status="REVIEW",
            common_storage_record=BidNoticeStorageRecord(
                bid_notice_no="R26BK0000000",
                bid_notice_ord="00",
            ),
            industry_restriction=EnrichmentCheck(
                state="FAIL",
                label="불일치: 3230, 3244",
            ),
        )

        classified = apply_industry_restriction_exclusion(item)

        self.assertEqual(classified.match_status, "EXCLUDE")
        decision = next(
            decision
            for decision in classified.column_decisions
            if decision.column == "업종제한(기관코드)"
        )
        self.assertEqual(decision.status, "FAIL")
        self.assertEqual(decision.detail, "불일치: 3230, 3244")

    def test_industry_mismatch_label_overrides_review_state(self):
        item = BidNoticePreviewItem(
            record_id="R26BK0000001-00",
            bid_notice_no="R26BK0000001",
            bid_notice_ord="00",
            detail_enrichment_status="DETAIL_COMPLETED",
            match_status="REVIEW",
            common_storage_record=BidNoticeStorageRecord(
                bid_notice_no="R26BK0000001",
                bid_notice_ord="00",
            ),
            industry_restriction=EnrichmentCheck(
                state="REVIEW",
                label="업종제한 조회결과 불일치: 3230, 3244",
            ),
        )

        classified = apply_industry_restriction_exclusion(item)

        self.assertEqual(classified.match_status, "EXCLUDE")


if __name__ == "__main__":
    unittest.main()
