import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch

from app.features.g2b_bid_notice.contracts import BidNoticeStorageRecord, KST
from app.features.g2b_bid_notice.schemas import (
    BidNoticePreviewItem,
    EnrichmentCheck,
    NoticeAttachmentSource,
)
from app.features.g2b_bid_notice.sheets import (
    LEGACY_SHEET_HEADERS,
    SHEET_HEADERS,
    _apply_attachment_hyperlinks,
    _attachment_link_text,
    append_selected_bid_notices,
    _deduplicate_items,
    _ensure_headers,
    _migrate_legacy_sheet_row,
    _sheet_row,
    _translate_google_error,
)


def make_item(ordinal: str = "00") -> BidNoticePreviewItem:
    return BidNoticePreviewItem(
        record_id=f"R26BK000001-{ordinal}",
        bid_notice_no="R26BK000001",
        bid_notice_ord=ordinal,
        business_name="AI 교육 운영 용역",
        demand_agency_name="서울특별시교육청",
        published_at=datetime(2026, 7, 20, 9, 0, tzinfo=KST),
        bid_closing_at=datetime(2026, 7, 25, 10, 0, tzinfo=KST),
        business_amount=None,
        detail_enrichment_status="LIST_ONLY",
        match_status="PRIORITY",
        common_storage_record=BidNoticeStorageRecord(
            bid_notice_no="R26BK000001",
            bid_notice_ord=ordinal,
            region_restriction=None,
        ),
    )


class SheetsExportTests(unittest.TestCase):
    def test_invalid_service_account_signature_has_actionable_error(self):
        error = _translate_google_error(
            RuntimeError("invalid_grant: Invalid JWT Signature.")
        )

        self.assertIn("서비스 계정 JSON 키", str(error))

    def test_sheet_headers_match_preview_columns_without_screen_only_fields(self):
        self.assertEqual(SHEET_HEADERS[0:4], ["공고명", "공고번호", "업무구분", "게시일시 / 입찰마감일시"])
        self.assertEqual(SHEET_HEADERS[4:8], ["수요기관", "세부절차", "세부절차상태", "사업금액"])
        self.assertNotIn("결과 분류", SHEET_HEADERS)
        self.assertNotIn("판정 사유", SHEET_HEADERS)
        self.assertNotIn("상세 확인 상태", SHEET_HEADERS)
        self.assertEqual(SHEET_HEADERS[-1], "Sheets 저장일시")

    def test_sheet_row_does_not_substitute_business_amount(self):
        row = _sheet_row(make_item(), "2026-07-20 10:00:00")

        self.assertEqual(row[0], "AI 교육 운영 용역")
        self.assertEqual(row[2], "확인 필요")
        self.assertEqual(row[3], "게시 2026-07-20 09:00:00\n입찰마감 2026-07-25 10:00:00")
        self.assertEqual(row[7], "")
        self.assertEqual(row[8], "확인 전")
        self.assertEqual(row[12], "첨부파일 링크 없음 또는 확인 필요")

    def test_legacy_sheet_row_preserves_data_in_new_preview_columns(self):
        migrated = _migrate_legacy_sheet_row(
            [
                "AI 교육 운영 용역",
                "R26BK000001",
                "00",
                "2026-07-20 09:00:00",
                "2026-07-25 10:00:00",
                "서울시교육청",
                "진행완료",
                1000000,
                "통과: 0036",
                "가능",
                "제한 없음",
                "https://example.test/notice",
                "request.pdf | https://example.test/request.pdf",
                "2026-07-20 10:00:00",
            ]
        )

        self.assertEqual(migrated[2], "")
        self.assertEqual(migrated[3], "게시 2026-07-20 09:00:00\n입찰마감 2026-07-25 10:00:00")
        self.assertEqual(migrated[4], "서울시교육청")
        self.assertEqual(migrated[7], 1000000)
        self.assertEqual(migrated[-1], "2026-07-20 10:00:00")

    def test_legacy_sheet_headers_are_migrated_before_new_rows_append(self):
        legacy_row = [
            "AI 교육 운영 용역",
            "R26BK000001",
            "00",
            "2026-07-20 09:00:00",
            "2026-07-25 10:00:00",
            "서울시교육청",
            "진행완료",
            1000000,
            "통과: 0036",
            "가능",
            "제한 없음",
            "https://example.test/notice",
            "request.pdf | https://example.test/request.pdf",
            "2026-07-20 10:00:00",
        ]
        service = MagicMock()
        service.spreadsheets().values().get().execute.side_effect = [
            {"values": [LEGACY_SHEET_HEADERS]},
            {"values": [legacy_row]},
        ]

        _ensure_headers(service, "spreadsheet-id", "G2B")

        update = service.spreadsheets().values().update.call_args.kwargs
        self.assertEqual(update["body"]["values"][0], SHEET_HEADERS)
        self.assertEqual(update["body"]["values"][1][3], "게시 2026-07-20 09:00:00\n입찰마감 2026-07-25 10:00:00")

    def test_dedup_uses_notice_number_even_when_orders_differ(self):
        unique_items = _deduplicate_items([make_item("00"), make_item("001")])

        self.assertEqual(len(unique_items), 1)
        self.assertEqual(unique_items[0].bid_notice_ord, "00")

    @patch("app.features.g2b_bid_notice.sheets._apply_attachment_hyperlinks")
    @patch("app.features.g2b_bid_notice.sheets._existing_bid_notice_numbers")
    @patch("app.features.g2b_bid_notice.sheets._ensure_headers")
    @patch("app.features.g2b_bid_notice.sheets._service")
    @patch("app.features.g2b_bid_notice.sheets._settings")
    def test_save_skips_duplicates_and_inserts_new_rows_below_the_header(
        self,
        settings,
        service_factory,
        ensure_headers,
        existing_notice_numbers,
        apply_attachment_hyperlinks,
    ):
        settings.return_value = ("spreadsheet-id", "G2B")
        service = MagicMock()
        service_factory.return_value = service
        existing_notice_numbers.return_value = {"r26bk000001"}
        service.spreadsheets().get().execute.return_value = {
            "sheets": [{"properties": {"sheetId": 7, "title": "G2B"}}]
        }
        service.spreadsheets().values().update().execute.return_value = {
            "updatedRange": "G2B!A2:N2"
        }
        new_item = make_item("001").model_copy(
            update={"record_id": "R26BK000002-001", "bid_notice_no": "R26BK000002"}
        )

        saved_count, skipped_count, updated_range = append_selected_bid_notices(
            [make_item(), new_item]
        )

        self.assertEqual((saved_count, skipped_count, updated_range), (1, 1, "G2B!A2:N2"))
        update = service.spreadsheets().values().update.call_args.kwargs
        self.assertEqual(update["range"], "G2B!A2")
        self.assertEqual(len(update["body"]["values"]), 1)
        self.assertEqual(update["body"]["values"][0][1], "R26BK000002")
        insert_request = service.spreadsheets().batchUpdate.call_args.kwargs["body"]["requests"][0]
        self.assertEqual(insert_request["insertDimension"]["range"], {
            "sheetId": 7,
            "dimension": "ROWS",
            "startIndex": 1,
            "endIndex": 2,
        })
        service.spreadsheets().values().append.assert_not_called()
        ensure_headers.assert_called_once()
        apply_attachment_hyperlinks.assert_called_once()

    def test_sheet_row_uses_attachment_source_urls_and_clickable_ranges(self):
        item = make_item().model_copy(
            update={
                "work_type": "일반용역",
                "detail_procedure": "공고등록",
                "detail_procedure_status": "진행완료",
                "industry_restriction": EnrichmentCheck(state="PASS", label="통과: 0036"),
                "joint_contracting": EnrichmentCheck(state="ALLOWED", label="가능"),
                "region_restriction_detail": EnrichmentCheck(state="NO_RESTRICTION", label="제한 없음"),
                "attachment_sources": [
                    NoticeAttachmentSource(
                        file_name="제안요청서.hwpx",
                        download_url="https://files.example.test/request.hwpx",
                        source_type="나라장터 첨부파일",
                    )
                ],
            }
        )

        row = _sheet_row(item, "2026-07-20 10:00:00")
        text, link_ranges = _attachment_link_text(item)

        self.assertEqual(row[8], "통과: 0036")
        self.assertEqual(row[9], "가능")
        self.assertEqual(row[10], "제한 없음")
        self.assertEqual(row[12], "제안요청서.hwpx | https://files.example.test/request.hwpx")
        self.assertEqual(text, row[12])
        self.assertEqual(
            link_ranges,
            [(0, len("제안요청서.hwpx"), "https://files.example.test/request.hwpx")],
        )

    def test_saved_attachment_filename_is_formatted_as_a_google_sheet_link(self):
        item = make_item().model_copy(
            update={
                "attachment_sources": [
                    NoticeAttachmentSource(
                        file_name="request.pdf",
                        download_url="https://files.example.test/request.pdf",
                        source_type="나라장터 첨부파일",
                    )
                ]
            }
        )
        service = MagicMock()
        service.spreadsheets().get().execute.return_value = {
            "sheets": [{"properties": {"sheetId": 123, "title": "G2B"}}]
        }

        _apply_attachment_hyperlinks(
            service,
            "spreadsheet-id",
            "G2B",
            "G2B!A7:N7",
            [item],
        )

        request = service.spreadsheets().batchUpdate.call_args.kwargs["body"]["requests"][0]
        cell = request["updateCells"]["rows"][0]["values"][0]
        self.assertEqual(
            cell["userEnteredValue"]["stringValue"],
            "request.pdf | https://files.example.test/request.pdf",
        )
        self.assertEqual(
            cell["textFormatRuns"][0]["format"]["link"]["uri"],
            "https://files.example.test/request.pdf",
        )


if __name__ == "__main__":
    unittest.main()
