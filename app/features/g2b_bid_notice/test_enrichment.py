import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch
from zipfile import ZipFile

from app.features.g2b_bid_notice.enrichment import (
    ExtractedAttachment,
    _NoticeDetailPageParser,
    _combine_structured_and_document_checks,
    _enrich_one,
    _cached_attachments,
    _detect_document_suffix,
    _extract_attachment_text,
    _extract_hwp_records,
    _industry_restriction_from_attachments,
    _industry_restriction,
    _joint_contracting,
    _region_restriction_from_attachments,
    _region_restriction,
    _sources_from_detail_page,
    _same_notice,
    attachment_sources_from_notice_item,
)
from app.features.g2b_bid_notice.contracts import BidNoticeStorageRecord
from app.features.g2b_bid_notice.schemas import BidNoticePreviewItem, EnrichmentCheck, LocalAttachmentFile


def _attachment(name: str = "notice.hwpx") -> LocalAttachmentFile:
    return LocalAttachmentFile(
        file_name=name,
        local_path=f"C:/temporary/{name}",
        source_type="test",
        extraction_status="TEXT_EXTRACTED",
        extraction_message="test",
    )


def _notice(order: str = "000") -> BidNoticePreviewItem:
    return BidNoticePreviewItem(
        record_id="test-notice",
        bid_notice_no="R26BK000001",
        bid_notice_ord=order,
        match_status="REVIEW",
        detail_enrichment_status="LIST_ONLY",
        common_storage_record=BidNoticeStorageRecord(
            bid_notice_no="R26BK000001",
            bid_notice_ord=order,
        ),
    )


class EnrichmentDecisionTests(TestCase):
    def test_notice_row_uses_all_numbered_detail_attachment_urls(self):
        sources = attachment_sources_from_notice_item(
            {
                "ntceSpecFileNm1": "과업지시서.hwp",
                "ntceSpecDocUrl1": "https://example.test/files/task.hwp",
                "ntceSpecFileNm2": "제안요청서.pdf",
                "ntceSpecDocUrl2": "https://example.test/files/rfp.pdf",
                "ntceSpecDocUrl3": "",
                # The standard notice document duplicates the first link and
                # must not cause a second download.
                "stdNtceDocUrl": "https://example.test/files/task.hwp",
            }
        )
        self.assertEqual([source.file_name for source in sources], ["과업지시서.hwp", "제안요청서.pdf"])
        self.assertEqual(len({source.download_url for source in sources}), 2)

    def test_detail_page_attachment_links_are_added_when_they_look_downloadable(self):
        parser = _NoticeDetailPageParser()
        parser.feed(
            '<html><body><p>입찰참가자격</p><a href="/download/rfp.pdf">제안요청서</a>'
            '<a href="javascript:void(0)">무시</a></body></html>'
        )
        sources = _sources_from_detail_page(parser, "https://www.g2b.go.kr/notice/123")
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0].file_name, "제안요청서")
        self.assertEqual(sources[0].download_url, "https://www.g2b.go.kr/download/rfp.pdf")

    def test_hwp_signature_is_read_even_when_g2b_omits_the_filename_extension(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "공고문"
            path.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"test")
            self.assertEqual(_detect_document_suffix(path), ".hwp")

    def test_binary_xls_signature_keeps_its_file_type(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "산출내역.xls"
            path.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"test")
            self.assertEqual(_detect_document_suffix(path), ".xls")

    def test_zip_attachment_reads_supported_inner_files(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "제안요청서.zip"
            with ZipFile(path, "w") as archive:
                archive.writestr("공동수급.txt", "공동수급을 허용하며 구성원은 5개 이하")
            extracted = _extract_attachment_text(
                LocalAttachmentFile(
                    file_name="제안요청서.zip",
                    local_path=str(path),
                    source_type="test",
                    extraction_status="TEXT_EXTRACTED",
                    extraction_message="test",
                )
            )
            self.assertEqual(extracted.attachment.extraction_status, "TEXT_EXTRACTED")
            self.assertIn("공동수급을 허용", extracted.text)

    def test_license_restriction_passes_when_one_allowed_code_is_present(self):
        check = _industry_restriction(
            [{"lcnsLmtNm": "정보통신공사업/0036, 소프트웨어사업자/1468"}], None
        )
        self.assertEqual(check.state, "PASS")
        self.assertEqual(check.label, "통과: 0036, 1468")

    def test_license_restriction_fails_when_only_other_code_is_present(self):
        check = _industry_restriction([{"lcnsLmtNm": "농업기계 사후관리업/7269"}], None)
        self.assertEqual(check.state, "FAIL")
        self.assertEqual(check.label, "불일치: 7269")

    def test_no_region_rows_means_no_public_api_region_restriction(self):
        check = _region_restriction([], None)
        self.assertEqual(check.state, "NO_RESTRICTION")
        self.assertEqual(check.label, "제한 없음")

    def test_joint_contracting_marks_allowed_with_conditions(self):
        check = _joint_contracting(
            [
                ExtractedAttachment(
                    attachment=_attachment(),
                    text=(
                        "본 사업은 공동수급을 허용하며, 공동수급업체 구성원은 관련 법령에 "
                        "적합해야 합니다. 공동수급체는 5개 이하로 구성하여야 하며, "
                        "구성원별 계약참여 최소지분율은 10% 이상으로 하여야 합니다."
                    ),
                )
            ],
            [],
        )
        self.assertEqual(check.state, "ALLOWED")
        self.assertEqual(check.label, "가능(조건 있음)")
        self.assertIn("notice.hwpx", check.evidence[0])

    def test_joint_contracting_conflict_requires_manual_review(self):
        check = _joint_contracting(
            [
                ExtractedAttachment(attachment=_attachment("request.hwp"), text="공동수급을 허용합니다."),
                ExtractedAttachment(attachment=_attachment("notice.hwp"), text="공동수급체 구성은 불가합니다."),
            ],
            [],
        )
        self.assertEqual(check.state, "REVIEW")
        self.assertEqual(check.label, "확인 필요 (공동수급 근거 상충)")
        self.assertEqual(len(check.evidence), 2)

    def test_joint_contracting_does_not_mistake_a_method_name_for_permission(self):
        check = _joint_contracting(
            [
                ExtractedAttachment(
                    attachment=_attachment(),
                    text="공동수급(공동이행방식 및 분담이행방식)은 허용하지 아니함.",
                )
            ],
            [],
        )
        self.assertEqual(check.state, "NOT_ALLOWED")
        self.assertEqual(check.label, "불가")

    def test_hwp_record_parser_reads_paragraph_text_record(self):
        content = "공동도급 가능".encode("utf-16le")
        header = (67 | (len(content) << 20)).to_bytes(4, "little")
        self.assertEqual(_extract_hwp_records(header + content), "공동도급 가능")

    def test_attachment_records_are_matched_by_notice_number_and_canonical_order(self):
        self.assertTrue(_same_notice({"bidNtceNo": "R26BK000001", "bidNtceOrd": "00"}, _notice("000")))
        self.assertFalse(_same_notice({"bidNtceNo": "R26BK999999", "bidNtceOrd": "00"}, _notice()))

    def test_attachment_text_can_pass_industry_code_and_mark_region_restriction(self):
        extracted = [
            ExtractedAttachment(
                attachment=_attachment(),
                text="입찰참가자격 업종제한: 정보통신공사업 0036. 지역제한: 서울특별시 소재 업체.",
            )
        ]

        self.assertEqual(_industry_restriction_from_attachments(extracted).state, "PASS")
        self.assertEqual(_region_restriction_from_attachments(extracted).state, "PASS")

    def test_labelled_industry_code_after_eligibility_heading_is_detected(self):
        extracted = [
            ExtractedAttachment(
                attachment=_attachment("제안요청서.pdf"),
                text=(
                    "입찰참가자격을 갖춘 사업자\n"
                    "소프트웨어사업자(컴퓨터관련서비스업[업종코드 1468])"
                ),
            )
        ]

        check = _industry_restriction_from_attachments(extracted)

        self.assertEqual(check.state, "PASS")
        self.assertEqual(check.label, "통과: 1468")

    def test_registered_address_eligibility_marks_region_restriction_without_literal_keyword(self):
        extracted = [
            ExtractedAttachment(
                attachment=_attachment("R26BK01646092_공고문.pdf"),
                text=(
                    "3. 입찰참가자격\n"
                    "사업자등록증 또는 관련 서류에 기재된 사업자의 소재지가 "
                    "경상북도에 있는 업체"
                ),
            )
        ]

        check = _region_restriction_from_attachments(extracted)

        self.assertEqual(check.state, "PASS")
        self.assertEqual(check.label, "제한: 경상북도")
        self.assertIn("소재지가 경상북도에 있는 업체", check.evidence[0])

    def test_registered_address_variants_mark_region_restriction(self):
        extracted = [
            ExtractedAttachment(
                attachment=_attachment("공고문.pdf"),
                text="입찰참가자격: 법인등기부상 본점 소재지가 서울특별시 내에 둔 자",
            ),
            ExtractedAttachment(
                attachment=_attachment("제안요청서.pdf"),
                text="입찰공고일 현재 사업자의 소재지가 전라남도인 업체",
            ),
        ]

        check = _region_restriction_from_attachments(extracted)

        self.assertEqual(check.state, "PASS")
        self.assertEqual(check.label, "제한: 서울특별시, 전라남도")

    def test_region_mentioned_as_project_location_does_not_create_a_region_restriction(self):
        extracted = [
            ExtractedAttachment(
                attachment=_attachment(),
                text="본 사업의 수행 장소는 경상북도이며, 사업 기간은 12개월입니다.",
            )
        ]

        check = _region_restriction_from_attachments(extracted)

        self.assertEqual(check.state, "REVIEW")

    def test_no_restriction_phrase_does_not_hide_a_later_residency_condition(self):
        extracted = [
            ExtractedAttachment(
                attachment=_attachment(),
                text=(
                    "지역제한 없음. 다만 입찰참가자의 사업자등록상 소재지가 "
                    "경상북도에 있는 업체여야 합니다."
                ),
            )
        ]

        check = _region_restriction_from_attachments(extracted)

        self.assertEqual(check.state, "REVIEW")
        self.assertEqual(check.label, "확인 필요 (지역제한 근거 상충)")

    def test_cached_attachment_is_analysed_when_a_requery_has_no_attachment_links(self):
        with tempfile.TemporaryDirectory() as directory:
            cache_root = Path(directory)
            attachment_dir = cache_root / "R26BK000001" / "000"
            attachment_dir.mkdir(parents=True)
            (attachment_dir / "공고문-a1b2c3d4e5.txt").write_text(
                "입찰참가자격: 사업자의 소재지가 경상북도에 있는 업체",
                encoding="utf-8",
            )
            with (
                patch("app.features.g2b_bid_notice.enrichment.ATTACHMENT_ROOT", cache_root),
                patch("app.features.g2b_bid_notice.enrichment._try_fetch", return_value=([], None)),
                patch("app.features.g2b_bid_notice.enrichment._fetch_notice_detail_page", return_value=("", [], None)),
                patch("app.features.g2b_bid_notice.enrichment._fallback_attachment_sources", return_value=([], [])),
            ):
                cached = _cached_attachments(_notice())
                enriched = _enrich_one(_notice())

        self.assertEqual(len(cached), 1)
        self.assertEqual(cached[0].file_name, "공고문.txt")
        self.assertEqual(enriched.region_restriction_detail.state, "PASS")
        self.assertEqual(enriched.region_restriction_detail.label, "제한: 경상북도")

    def test_region_notice_reference_stays_review_until_a_document_has_the_actual_region(self):
        extracted = [
            ExtractedAttachment(attachment=_attachment(), text="지역제한: 공고서 참조"),
        ]
        check = _region_restriction_from_attachments(extracted)
        self.assertEqual(check.state, "REVIEW")
        self.assertEqual(check.label, "확인 필요 (공고서 참조)")

    def test_structured_and_document_conflict_is_not_silently_resolved(self):
        check = _combine_structured_and_document_checks(
            "업종제한",
            EnrichmentCheck(state="PASS", label="통과: 1468", evidence=["상세 화면: 1468"]),
            EnrichmentCheck(state="FAIL", label="불일치: 7269", evidence=["첨부파일: 7269"]),
        )
        self.assertEqual(check.state, "REVIEW")
        self.assertEqual(check.label, "확인 필요 (업종제한 근거 상충)")
