from datetime import date, datetime, time
from decimal import Decimal
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator

from app.g2b.bid_notice import canonical_bid_notice_order, parse_g2b_datetime
from app.g2b.keyword_policy import normalize_keywords


KST = ZoneInfo("Asia/Seoul")


class PreSpecificationTransfer(BaseModel):
    """bfSpecRgstNo를 원본 식별자로 사용하는 내부 전달 계약."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    bf_spec_rgst_no: str = Field(
        validation_alias=AliasChoices("bf_spec_rgst_no", "bfSpecRgstNo"),
        min_length=1,
        max_length=80,
    )
    bid_notice_no: str | None = None
    bid_notice_ord: str | None = None
    reference_no: str | None = Field(
        default=None,
        validation_alias=AliasChoices("reference_no", "refNo"),
    )
    business_name: str | None = None
    business_type: str | None = None
    demand_agency_name: str | None = None
    ordering_agency_name: str | None = None
    allocated_budget: Decimal | None = None
    registered_at: datetime | None = None
    opinion_deadline: datetime | None = None
    delivery_deadline: datetime | None = None
    contact_name: str | None = None
    contact_phone: str | None = None
    attachments: list[dict] = Field(default_factory=list)
    raw: dict = Field(default_factory=dict)

    @field_validator(
        "bf_spec_rgst_no",
        "bid_notice_no",
        "bid_notice_ord",
        "reference_no",
        "business_name",
        "business_type",
        "demand_agency_name",
        "ordering_agency_name",
        "contact_name",
        "contact_phone",
        mode="before",
    )
    @classmethod
    def clean_text(cls, value):
        text = str(value or "").strip()
        return text or None

    @field_validator(
        "registered_at",
        "opinion_deadline",
        "delivery_deadline",
        mode="before",
    )
    @classmethod
    def parse_datetime(cls, value):
        return parse_g2b_datetime(value)

    @model_validator(mode="after")
    def validate_bid_reference(self):
        if bool(self.bid_notice_no) != bool(self.bid_notice_ord):
            raise ValueError("연결 공고번호와 차수는 함께 전달해야 합니다.")
        if self.bid_notice_ord:
            self.bid_notice_ord = canonical_bid_notice_order(self.bid_notice_ord)
        return self


class CollectPreSpecificationsRequest(BaseModel):
    start_date: date
    end_date: date

    @model_validator(mode="after")
    def validate_window(self):
        if self.end_date < self.start_date:
            raise ValueError("end_date는 start_date보다 빠를 수 없습니다.")
        if (self.end_date - self.start_date).days > 31:
            raise ValueError("한 번의 수집 기간은 최대 31일입니다.")
        return self


class CollectPreSpecificationsResponse(BaseModel):
    run_key: str
    fetched_count: int
    inserted_count: int
    updated_count: int


class PreSpecificationListQuery(BaseModel):
    q: str | None = None
    keywords: list[str] = Field(default_factory=list)
    keyword_mode: Literal["AND", "OR"] = "OR"
    excluded_keywords: list[str] = Field(default_factory=list)
    registered_from: date | None = None
    registered_to: date | None = None
    demand_agency: str | None = None
    min_budget: Decimal | None = Field(default=None, ge=0)
    max_budget: Decimal | None = Field(default=None, ge=0)
    attachment: Literal["ALL", "HAS", "NONE"] = "ALL"
    deadline_status: Literal["ALL", "OPEN", "TODAY", "CLOSED", "UNKNOWN"] = "ALL"
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=30, ge=1, le=100)

    @field_validator("keywords", "excluded_keywords", mode="before")
    @classmethod
    def clean_keywords(cls, values):
        return normalize_keywords(values)

    @model_validator(mode="after")
    def validate_budget(self):
        if (
            self.min_budget is not None
            and self.max_budget is not None
            and self.min_budget > self.max_budget
        ):
            raise ValueError("min_budget은 max_budget보다 클 수 없습니다.")
        return self


class PreSpecificationResponse(BaseModel):
    bf_spec_rgst_no: str
    bid_notice_no: str | None
    bid_notice_ord: str | None
    reference_no: str | None
    business_name: str | None
    business_type: str | None
    demand_agency_name: str | None
    ordering_agency_name: str | None
    allocated_budget: Decimal | None
    registered_at: datetime | None
    opinion_deadline: datetime | None
    delivery_deadline: datetime | None
    contact_name: str | None
    contact_phone: str | None
    attachments: list[dict] = Field(default_factory=list)
    deadline_status: Literal["OPEN", "TODAY", "CLOSED", "UNKNOWN"]
    first_seen_at: datetime
    last_seen_at: datetime


class PreSpecificationListResponse(BaseModel):
    items: list[PreSpecificationResponse]
    total: int
    page: int
    page_size: int


class ArchivedPreSpecificationResponse(PreSpecificationResponse):
    handled_state: Literal["DISMISSED", "EXPORTED"]
    handled_at: datetime
    expires_at: datetime
    can_restore: bool


class ArchivedPreSpecificationListResponse(BaseModel):
    items: list[ArchivedPreSpecificationResponse]
    total: int
    page: int
    page_size: int


class DismissPreSpecificationResponse(BaseModel):
    bf_spec_rgst_no: str
    state: Literal["DISMISSED"] = "DISMISSED"


class RestorePreSpecificationResponse(BaseModel):
    bf_spec_rgst_no: str
    state: Literal["RESTORED"] = "RESTORED"
    visible: bool


class ExportPreSpecificationsSheetRequest(BaseModel):
    bf_spec_rgst_nos: list[str] = Field(min_length=1, max_length=100)
    destination_id: int = Field(ge=1)
    dry_run: bool = True
    expected_preview_token: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )

    @field_validator("bf_spec_rgst_nos")
    @classmethod
    def deduplicate_ids(cls, values: list[str]) -> list[str]:
        normalized = list(
            dict.fromkeys(str(value).strip() for value in values if str(value).strip())
        )
        if not normalized:
            raise ValueError("사전규격 등록번호를 하나 이상 선택해야 합니다.")
        return normalized


class ExportPreSpecificationsSheetResponse(BaseModel):
    headers: list[str]
    requested_count: int
    row_count: int
    written: bool
    inserted_count: int
    updated_count: int
    preview_rows: list[list[str | int | float]]
    destination_id: int
    destination_label: str
    destination_scope: Literal["PERSONAL", "ORGANIZATION"]
    destination_tab_name: str
    preview_token: str


def date_window(start: date, end: date) -> tuple[datetime, datetime]:
    return (
        datetime.combine(start, time.min, tzinfo=KST),
        datetime.combine(end, time.max, tzinfo=KST),
    )
