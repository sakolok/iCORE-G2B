import unittest

from app.features.g2b_bid_notice.contracts import (
    KST,
    bid_notice_dedup_key,
    map_bid_notice_api_item,
)


class BidNoticeContractTests(unittest.TestCase):
    def test_dedup_keeps_raw_ordinal_but_compares_zero_padding_as_equal(self):
        self.assertEqual(
            bid_notice_dedup_key("R26BK000001", "00"),
            bid_notice_dedup_key("R26BK000001", "000"),
        )

    def test_maps_only_direct_common_contract_fields(self):
        record = map_bid_notice_api_item(
            {
                "bidNtceNo": "R26BK000001",
                "bidNtceOrd": "000",
                "bidNtceNm": "AI 교육 운영 용역",
                "dminsttNm": "서울특별시교육청",
                "bsisAmount": "1,230,000",
                "prpslSbmtnEndDt": "202607161530",
                "rgnLmtYn": "Y",
                "twoStageBidYn": "N",
                "presmptPrce": "9,999,999",
                "bidClseDt": "202607171000",
            }
        )

        self.assertEqual(record.bid_notice_ord, "000")
        self.assertEqual(record.base_amount, 1_230_000)
        self.assertEqual(record.proposal_deadline.tzinfo, KST)
        self.assertTrue(record.region_restriction)
        self.assertFalse(record.is_two_stage_bid)

    def test_does_not_substitute_estimated_price_or_bid_closing_time(self):
        record = map_bid_notice_api_item(
            {
                "bidNtceNo": "R26BK000002",
                "bidNtceOrd": "00",
                "presmptPrce": "1,000,000",
                "bidClseDt": "202607171000",
            }
        )

        self.assertIsNone(record.base_amount)
        self.assertIsNone(record.proposal_deadline)

    def test_keeps_a_direct_zero_amount_as_a_number(self):
        record = map_bid_notice_api_item({"bidNtceNo": "R26BK000003", "bsisAmount": 0})

        self.assertEqual(record.base_amount, 0)


if __name__ == "__main__":
    unittest.main()
