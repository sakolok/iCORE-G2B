from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.g2b.keyword_policy import normalize_keywords


class CollectBidNoticesRequest(BaseModel):
    start_date: date
    end_date: date
    business_types: list[Literal["SERVICE", "GOODS", "CONSTRUCTION"]] = Field(
        default_factory=lambda: ["SERVICE"]
    )

    @model_validator(mode="after")
    def validate_window(self):
        if self.end_date < self.start_date:
            raise ValueError("종료일은 시작일보다 빠를 수 없습니다.")
        if (self.end_date - self.start_date).days > 14:
            raise ValueError("한 번의 수집 기간은 최대 14일입니다.")
        if not self.business_types:
            raise ValueError("최소 한 가지 업무구분을 선택하세요.")
        self.business_types = list(dict.fromkeys(self.business_types))
        return self


class CollectBidNoticesResponse(BaseModel):
    run_key: str
    fetched_count: int
    inserted_count: int
    updated_count: int


class BidNoticeProfileUpdateRequest(BaseModel):
    enabled: bool
    keywords: list[str] = Field(default_factory=list)
    excluded_keywords: list[str] = Field(default_factory=list)

    @field_validator("keywords", "excluded_keywords", mode="before")
    @classmethod
    def normalize_keyword_values(cls, values):
        return normalize_keywords(values)


class BidNoticeProfileResponse(BidNoticeProfileUpdateRequest):
    pass


class BidNoticeSettingsResponse(BaseModel):
    profile: BidNoticeProfileResponse


class BidNoticeListItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    bid_notice_no: str | None
    bid_notice_ord: str | None
    business_name: str | None
    demand_agency_name: str | None
    work_type: str | None
    procurement_type: str | None
    official_base_amount: Decimal | None
    business_amount: Decimal | None = None
    published_at: datetime | None
    deadline_at: datetime | None
    notice_url: str | None
    region_restriction: str | None
    region_restriction_api_status: str | None
    is_two_stage_bid: bool | None
    matched_keyword: str | None = None

    @field_validator("published_at", "deadline_at", mode="before")
    @classmethod
    def timezone_for_legacy_rows(cls, value):
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value


class BidNoticeListResponse(BaseModel):
    items: list[BidNoticeListItem]
    total: int
    page: int
    page_size: int
