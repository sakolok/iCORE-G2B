from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.g2b.bid_notice import RegionRestrictionApiStatus
from app.g2b.keyword_policy import normalize_keywords


def _utc_if_naive(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _kst_if_naive(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=ZoneInfo("Asia/Seoul"))
    return value


class BusinessType(str, Enum):
    SERVICE = "SERVICE"
    GOODS = "GOODS"
    CONSTRUCTION = "CONSTRUCTION"
    FOREIGN = "FOREIGN"


class OpeningStatus(str, Enum):
    OPENED = "OPENED"
    AWARDED = "AWARDED"
    FAILED = "FAILED"
    REBID = "REBID"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"


class CollectOpeningResultsRequest(BaseModel):
    start_at: datetime
    end_at: datetime
    business_type: BusinessType = BusinessType.SERVICE
    include_entries: bool = True

    @model_validator(mode="after")
    def validate_window(self):
        if self.start_at.tzinfo is None:
            self.start_at = self.start_at.replace(tzinfo=ZoneInfo("Asia/Seoul"))
        if self.end_at.tzinfo is None:
            self.end_at = self.end_at.replace(tzinfo=ZoneInfo("Asia/Seoul"))
        if self.end_at < self.start_at:
            raise ValueError("end_at은 start_at보다 빠를 수 없습니다.")
        if self.end_at - self.start_at > timedelta(days=14):
            raise ValueError("한 번의 수집 기간은 최대 14일입니다.")
        return self


class CollectOpeningResultsResponse(BaseModel):
    fetched_round_count: int
    fetched_entry_count: int
    inserted_round_count: int
    updated_round_count: int
    inserted_entry_count: int
    updated_entry_count: int
    skipped_count: int


class ScheduledCollectOpeningResultsResponse(CollectOpeningResultsResponse):
    run_key: str
    window_start: datetime
    window_end: datetime
    run_status: Literal["RUNNING", "SUCCESS", "FAILED"]
    skipped_existing_run: bool

    @field_validator("window_start", "window_end", mode="before")
    @classmethod
    def ensure_window_timezone(cls, value):
        return _utc_if_naive(value)


class NoticeEnrichmentRunResponse(BaseModel):
    enqueued_count: int
    claimed_count: int
    succeeded_count: int
    needs_review_count: int
    retry_scheduled_count: int


class OpeningEntryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    rank: int | None
    business_no: str | None
    company_name: str | None
    ceo_name: str | None
    bid_amount: Decimal | None
    bid_rate: Decimal | None
    bid_price_score: Decimal | None
    technical_raw_score: Decimal | None
    technical_score: Decimal | None
    total_score: Decimal | None
    official_total_score: Decimal | None
    bid_at: datetime | None
    note: str | None
    is_winner: bool

    @field_validator("bid_at", mode="before")
    @classmethod
    def ensure_bid_timezone(cls, value):
        return _utc_if_naive(value)


class OpeningResultSummaryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    business_type: str
    bid_notice_no: str
    bid_notice_ord: str
    bid_class_no: str
    rebid_no: str
    title: str | None
    business_name: str | None = None
    status: str
    status_label: str | None
    opened_at: datetime | None
    participant_count: int | None
    notice_agency_name: str | None
    demand_agency_name: str | None
    base_amount: Decimal | None = None
    prearranged_price_decision_method: str | None = None
    proposal_deadline: datetime | None = None
    region_restriction: str | None = None
    region_restriction_api_status: RegionRestrictionApiStatus | None = None
    is_two_stage_bid: bool | None = None
    notice_url: str | None = None
    sheet_export_status: Literal[
        "READY",
        "DETAIL_PENDING",
        "NOTICE_CONTEXT_MISSING",
        "NOTICE_CONTEXT_AMBIGUOUS",
    ] = "NOTICE_CONTEXT_MISSING"
    sheet_exportable: bool = False
    sheet_block_reasons: list[str] = Field(default_factory=list)
    first_rank_company_name: str | None = None
    first_rank_bid_price_score: Decimal | None = None
    first_rank_technical_score: Decimal | None = None
    winner_business_no: str | None
    winner_company_name: str | None
    winning_amount: Decimal | None
    winning_rate: Decimal | None
    final_awarded_at: datetime | None
    entries_collected_at: datetime | None
    collected_at: datetime
    matched_keywords: list[str] = Field(default_factory=list)

    @field_validator(
        "opened_at",
        "final_awarded_at",
        "entries_collected_at",
        "collected_at",
        mode="before",
    )
    @classmethod
    def ensure_result_timezone(cls, value):
        return _utc_if_naive(value)

    @field_validator("proposal_deadline", mode="before")
    @classmethod
    def ensure_deadline_timezone(cls, value):
        return _kst_if_naive(value)


class OpeningResultDetailResponse(OpeningResultSummaryResponse):
    opening_notice: str | None
    entries: list[OpeningEntryResponse]


class OpeningResultListResponse(BaseModel):
    items: list[OpeningResultSummaryResponse]
    total: int
    page: int
    page_size: int


class ArchivedOpeningResultSummaryResponse(OpeningResultSummaryResponse):
    handled_state: Literal["DISMISSED", "EXPORTED"]
    handled_at: datetime
    expires_at: datetime
    can_restore: bool

    @field_validator("handled_at", "expires_at", mode="before")
    @classmethod
    def ensure_archive_timezone(cls, value):
        return _utc_if_naive(value)


class ArchivedOpeningResultDetailResponse(ArchivedOpeningResultSummaryResponse):
    opening_notice: str | None
    entries: list[OpeningEntryResponse]


class ArchivedOpeningResultListResponse(BaseModel):
    items: list[ArchivedOpeningResultSummaryResponse]
    total: int
    page: int
    page_size: int


class OpeningResultListQuery(BaseModel):
    q: str | None = None
    status: OpeningStatus | None = None
    opened_from: datetime | None = None
    opened_to: datetime | None = None
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=30, ge=1, le=100)


class BidNoticeSheetContext(BaseModel):
    bid_notice_no: str
    bid_notice_ord: str = "00"
    business_name: str | None = None
    demand_agency_name: str | None = None
    base_amount: Decimal | None = None
    prearranged_price_decision_method: str | None = None
    proposal_deadline: datetime | None = None
    region_restriction: str | None = None
    region_restriction_api_status: RegionRestrictionApiStatus | None = None
    is_two_stage_bid: bool | None = None
    notice_url: str | None = None


class ExportOpeningResultsSheetRequest(BaseModel):
    result_ids: list[int] = Field(min_length=1, max_length=100)
    destination_id: int | None = Field(default=None, ge=1)
    tab_name: str | None = Field(
        default=None,
        deprecated=True,
        description="호환성을 위해 수용하지만 등록된 Sheet 목적지의 탭 이름을 사용합니다.",
    )
    dry_run: bool = True
    expected_preview_token: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
        description="dry_run 미리보기 응답의 토큰. 실제 반영 시 필수입니다.",
    )
    notice_contexts: list[BidNoticeSheetContext] = Field(
        default_factory=list,
        deprecated=True,
        description="호환성을 위해 수용하지만 Sheet 공식값에는 사용하지 않습니다.",
    )

    @field_validator("result_ids")
    @classmethod
    def deduplicate_result_ids(cls, values: list[int]) -> list[int]:
        return list(dict.fromkeys(values))


class ExportOpeningResultsSheetResponse(BaseModel):
    headers: list[str]
    requested_result_count: int
    row_count: int
    missing_result_ids: list[int]
    missing_notice_context_count: int
    missing_notice_context_keys: list[str]
    written: bool
    inserted_count: int
    updated_count: int
    preview_rows: list[list[str | int | float]]
    destination_id: int
    destination_label: str
    destination_scope: Literal["PERSONAL"]
    destination_tab_name: str
    preview_token: str


class OpeningResultProfileUpdateRequest(BaseModel):
    enabled: bool = True
    keywords: list[str] = Field(default_factory=list, max_length=100)
    excluded_keywords: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("keywords", "excluded_keywords", mode="before")
    @classmethod
    def normalize_keyword_values(cls, values):
        return normalize_keywords(values)

    @model_validator(mode="after")
    def validate_enabled_keywords(self):
        if self.enabled and not self.keywords:
            raise ValueError("활성화할 때는 포함 키워드를 한 개 이상 입력해야 합니다.")
        return self


class OpeningResultProfileResponse(BaseModel):
    enabled: bool
    keywords: list[str]
    excluded_keywords: list[str]


class SheetDestinationUpsertRequest(BaseModel):
    destination_id: int | None = Field(default=None, ge=1)
    label: str = Field(min_length=1, max_length=120)
    spreadsheet_id: str = Field(min_length=1, max_length=240)
    tab_name: str = Field(default="개찰결과", min_length=1, max_length=120)
    is_default: bool = True

    @field_validator("label", "spreadsheet_id", "tab_name")
    @classmethod
    def strip_destination_values(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("빈 값은 사용할 수 없습니다.")
        return cleaned


class SheetDestinationVerifyRequest(BaseModel):
    spreadsheet_id: str = Field(min_length=1, max_length=500)
    tab_name: str = Field(default="개찰결과", min_length=1, max_length=120)

    @field_validator("spreadsheet_id", "tab_name")
    @classmethod
    def strip_verify_values(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("빈 값은 사용할 수 없습니다.")
        return cleaned


class SheetDestinationVerifyResponse(BaseModel):
    spreadsheet_id: str
    spreadsheet_title: str | None
    tab_name: str
    tab_exists: bool
    header_status: Literal["MATCH", "EMPTY", "MISMATCH", "NOT_CHECKED"]
    connection_ready: bool
    sheet_service_account_email: str | None = None


class SheetDestinationResponse(BaseModel):
    id: int
    label: str
    spreadsheet_id: str
    tab_name: str
    scope: Literal["PERSONAL"]
    is_default: bool


class OpeningResultSettingsResponse(BaseModel):
    organization_id: int
    organization_name: str
    organization_role: str
    sheet_service_account_email: str | None = None
    profile: OpeningResultProfileResponse
    sheet_destinations: list[SheetDestinationResponse]


class DismissOpeningResultResponse(BaseModel):
    result_id: int
    state: Literal["DISMISSED"] = "DISMISSED"


class RestoreOpeningResultResponse(BaseModel):
    result_id: int
    state: Literal["RESTORED"] = "RESTORED"
    visible: bool
