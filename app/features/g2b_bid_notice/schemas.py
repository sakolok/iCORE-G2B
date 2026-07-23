from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.features.g2b_bid_notice.contracts import BidNoticeStorageRecord


class OrganizationGroup(BaseModel):
    """A user-managed group; it is not a G2B official classification."""

    id: str = Field(min_length=1, max_length=120)
    name: str = Field(min_length=1, max_length=120)
    parent_agencies: list[str] = Field(default_factory=list)
    child_agencies: list[str] = Field(default_factory=list)
    aliases: list[str] = Field(default_factory=list)
    agency_codes: list[str] = Field(default_factory=list)


class PersonalCollectionSettings(BaseModel):
    """One user's independently managed bid-notice collection setting."""

    name: str = Field(default="내 입찰공고 수집 설정", min_length=1, max_length=120)
    memo: str = Field(default="", max_length=500)
    organization_groups: list[OrganizationGroup] = Field(default_factory=list)
    work_types: list[Literal["물품", "민간물품", "일반용역", "기술용역", "공사"]] = Field(default_factory=list)
    procurement_types: list[Literal["내자", "외자"]] = Field(default_factory=list)
    required_title_keywords: list[str] = Field(default_factory=list)
    excluded_title_keywords: list[str] = Field(default_factory=list)
    base_amount_min: int | None = Field(default=None, ge=0)
    base_amount_max: int | None = Field(default=None, ge=0)
    participation_regions: list[str] = Field(default_factory=list)
    posted_date_start: date | None = None
    posted_date_end: date | None = None
    recipient_emails: list[str] = Field(default_factory=list)
    instant_priority_alert: bool = True
    review_digest_time: str | None = Field(default=None, max_length=5)
    google_sheet_target: str | None = Field(default=None, max_length=500)


class BidNoticeCandidate(BaseModel):
    """Raw-column candidate used only inside this feature before common storage."""

    bid_notice_no: str | None = None
    bid_notice_ord: str | None = None
    bid_ntce_nm: str | None = None
    demand_agency_name: str | None = None
    demand_agency_code: str | None = None
    work_type: str | None = None
    procurement_type: str | None = None
    base_amount: int | None = None
    participation_regions: list[str] | None = None
    proposal_deadline: datetime | None = None
    published_at: datetime | None = None
    bid_closing_at: datetime | None = None
    progress_status: str | None = None
    detail_procedure: str | None = None
    detail_procedure_status: str | None = None
    # This field is intentionally separate from base_amount.  It is filled only
    # when the source itself labels a value as a business/project amount.
    business_amount: int | None = None
    source_url: str | None = None
    detail_enrichment_status: Literal["LIST_ONLY", "DETAIL_REQUIRED", "DETAIL_COMPLETED", "SOURCE_MISSING"] = "LIST_ONLY"


class ColumnDecision(BaseModel):
    column: str
    status: Literal["PASS", "FAIL", "PENDING", "INACTIVE"]
    detail: str


class BidNoticePreviewRequest(BaseModel):
    collection_setting: PersonalCollectionSettings
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=50, ge=1, le=100)
    # This is intentionally a preview-only setting.  It is not part of the
    # common storage contract used when the separate collectors are merged.
    test_result_limit: int | None = Field(default=None, ge=1, le=100)


class EnrichmentCheck(BaseModel):
    """A source-backed check used only by this isolated collector feature."""

    state: Literal[
        "NOT_CHECKED",
        "PASS",
        "FAIL",
        "NO_RESTRICTION",
        "ALLOWED",
        "NOT_ALLOWED",
        "REVIEW",
    ] = "NOT_CHECKED"
    label: str = "확인 전"
    evidence: list[str] = Field(default_factory=list)


class LocalAttachmentFile(BaseModel):
    """A notice attachment stored only on this PC, never in Google Drive."""

    file_name: str
    local_path: str
    source_type: str
    extraction_status: Literal[
        "TEXT_EXTRACTED",
        "OCR_REQUIRED",
        "UNSUPPORTED",
        "DOWNLOAD_FAILED",
        "EXTRACTION_FAILED",
    ]
    extraction_message: str


class NoticeAttachmentSource(BaseModel):
    """One public G2B attachment entry indexed during the collection query."""

    file_name: str
    download_url: str
    source_type: str


class BidNoticePreviewItem(BaseModel):
    record_id: str
    source_type: Literal["BID_NOTICE"] = "BID_NOTICE"
    bid_notice_no: str | None = None
    bid_notice_ord: str | None = None
    business_name: str | None = None
    demand_agency_name: str | None = None
    work_type: str | None = None
    procurement_type: str | None = None
    base_amount: int | None = None
    participation_regions: list[str] | None = None
    proposal_deadline: datetime | None = None
    published_at: datetime | None = None
    bid_closing_at: datetime | None = None
    progress_status: str | None = None
    detail_procedure: str | None = None
    detail_procedure_status: str | None = None
    business_amount: int | None = None
    source_url: str | None = None
    detail_enrichment_status: Literal["LIST_ONLY", "DETAIL_REQUIRED", "DETAIL_COMPLETED", "SOURCE_MISSING"]
    match_status: Literal["PRIORITY", "REVIEW", "EXCLUDE"]
    column_decisions: list[ColumnDecision] = Field(default_factory=list)
    common_storage_record: BidNoticeStorageRecord
    industry_restriction: EnrichmentCheck = Field(default_factory=EnrichmentCheck)
    joint_contracting: EnrichmentCheck = Field(default_factory=EnrichmentCheck)
    region_restriction_detail: EnrichmentCheck = Field(default_factory=EnrichmentCheck)
    attachment_sources: list[NoticeAttachmentSource] = Field(default_factory=list)
    attachment_lookup_label: str = "확인 전"
    attachments: list[LocalAttachmentFile] = Field(default_factory=list)
    enrichment_checked_at: datetime | None = None


class BidNoticePreviewResponse(BaseModel):
    summary: dict[str, int]
    items: list[BidNoticePreviewItem]
    page: int
    page_size: int
    total_count: int


class SelectedBidNoticesSaveRequest(BaseModel):
    """User-confirmed rows that may be appended to the configured Sheet.

    The browser can only submit records it received from the preview endpoint.
    This local-only request remains separate from the common database contract.
    """

    collection_setting_name: str = Field(min_length=1, max_length=120)
    selected_items: list[BidNoticePreviewItem] = Field(min_length=1, max_length=100)


class SelectedBidNoticesSaveResponse(BaseModel):
    saved_count: int
    skipped_duplicate_count: int
    updated_range: str
