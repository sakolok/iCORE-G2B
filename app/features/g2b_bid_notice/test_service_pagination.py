from unittest import TestCase
from unittest.mock import patch

from app.features.g2b_bid_notice.service import (
    _fetch_latest_preview_window,
    _fetch_remaining_pages,
    preview_bid_notices,
)
from app.features.g2b_bid_notice.schemas import (
    BidNoticePreviewRequest,
    PersonalCollectionSettings,
)


def _payload(total_count: int, items: list[dict[str, str]]) -> dict:
    return {
        "response": {
            "header": {"resultCode": "00"},
            "body": {"totalCount": str(total_count), "items": {"item": items}},
        }
    }


class _Response:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self.payload


class FetchRemainingPagesTests(TestCase):
    @patch("app.features.g2b_bid_notice.service.requests.get")
    def test_reads_all_pages_reported_by_the_api(self, get):
        page_two = [{"bidNtceNo": f"two-{index}"} for index in range(100)]
        page_three = [{"bidNtceNo": f"three-{index}"} for index in range(32)]
        get.side_effect = [_Response(_payload(232, page_two)), _Response(_payload(232, page_three))]

        first_page = [{"bidNtceNo": f"one-{index}"} for index in range(100)]
        items, trace = _fetch_remaining_pages(
            operation_base_url="https://example.test",
            operation="notices",
            first_page_params={"numOfRows": 100, "pageNo": 1},
            first_page_items=first_page,
            reported_total_count=232,
        )

        self.assertEqual(len(items), 232)
        self.assertEqual(trace["page_count"], 3)
        self.assertEqual(trace["reported_total_count"], 232)
        self.assertEqual([call.kwargs["params"]["pageNo"] for call in get.call_args_list], [2, 3])

    @patch("app.features.g2b_bid_notice.service.requests.get")
    def test_test_preview_reads_the_final_page_for_latest_notices(self, get):
        first_page = [{"bidNtceNo": f"old-{index}"} for index in range(100)]
        final_page = [{"bidNtceNo": f"latest-{index}"} for index in range(37)]
        get.return_value = _Response(_payload(237, final_page))

        items, trace = _fetch_latest_preview_window(
            operation_base_url="https://example.test",
            operation="notices",
            first_page_params={"numOfRows": 100, "pageNo": 1},
            first_page_items=first_page,
            reported_total_count=237,
            result_limit=10,
        )

        self.assertEqual(len(items), 37)
        self.assertEqual(items[0]["bidNtceNo"], "latest-0")
        self.assertEqual(items[-1]["bidNtceNo"], "latest-36")
        self.assertEqual(trace["page_count"], 2)
        self.assertEqual([call.kwargs["params"]["pageNo"] for call in get.call_args_list], [3])


class PreviewEnrichmentTests(TestCase):
    @patch("app.features.g2b_bid_notice.service.enrich_bid_notice_items")
    @patch("app.features.g2b_bid_notice.service._fetch_bid_notices")
    def test_preview_enriches_every_notice_before_returning_items(self, fetch_bid_notices, enrich_items):
        fetch_bid_notices.return_value = [
            {
                "bidNtceNo": "R26BK0000001",
                "bidNtceOrd": "00",
                "bidNtceNm": "AI 교육 운영 용역",
                "dminsttNm": "테스트 교육청",
                "bidNtceDt": "202607220900",
                "bidClseDt": "202607291000",
            },
            {
                "bidNtceNo": "R26BK0000002",
                "bidNtceOrd": "000",
                "bidNtceNm": "AI 교육 콘텐츠 제작",
                "dminsttNm": "테스트 대학교",
                "bidNtceDt": "202607221000",
                "bidClseDt": "202607301000",
            },
        ]
        enrich_items.side_effect = lambda items: items

        response = preview_bid_notices(
            BidNoticePreviewRequest(
                collection_setting=PersonalCollectionSettings(
                    required_title_keywords=["AI"],
                ),
                test_result_limit=10,
            )
        )

        self.assertEqual(response.summary["fetched_count"], 2)
        self.assertEqual(enrich_items.call_count, 1)
        self.assertEqual(len(enrich_items.call_args.args[0]), 2)
