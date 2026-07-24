from datetime import datetime, time
from decimal import Decimal
from typing import Literal, Optional

from pydantic import BaseModel, EmailStr, Field, field_validator

from app.g2b.bid_notice import RegionRestrictionApiStatus
from app.g2b.keyword_policy import normalize_keywords


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=100)
    password: str = Field(..., min_length=1, max_length=200)


class GoogleLoginRequest(BaseModel):
    credential: str = Field(..., min_length=1, max_length=10000)


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "Bearer"
    user_id: int
    username: str
    email: str | None = None
    display_name: str | None = None
    role: str
    organization_id: int
    organization_name: str
    organization_role: str


class SessionResponse(BaseModel):
    user_id: int
    username: str
    email: str | None = None
    display_name: str | None = None
    role: str
    organization_id: int
    organization_name: str
    organization_role: str


class SchedulerStatus(BaseModel):
    configured: bool
    connected: bool
    applied: bool
    paused: bool
    schedule: str
    job_name: str
    target_url: str
    message: str


class ScraperNotice(BaseModel):
    notice_id: str = ""
    title: str = Field(..., min_length=1, max_length=500)
    agency: str = ""
    estimated_price: str = ""
    published_at: Optional[datetime] = None
    deadline_at: Optional[datetime] = None
    notice_url: str = ""
    bid_notice_no: Optional[str] = Field(default=None, max_length=160)
    bid_notice_ord: Optional[str] = Field(default=None, max_length=20)
    business_name: Optional[str] = Field(default=None, max_length=500)
    demand_agency_name: Optional[str] = Field(default=None, max_length=240)
    base_amount: Optional[Decimal] = None
    prearranged_price_decision_method: Optional[str] = Field(
        default=None,
        max_length=120,
    )
    proposal_deadline: Optional[datetime] = None
    region_restriction: Optional[str] = None
    region_restriction_api_status: Optional[RegionRestrictionApiStatus] = None
    is_two_stage_bid: Optional[bool] = None


class ScraperRunSummary(BaseModel):
    run_id: str
    status: Literal["success", "partial", "failed"]
    keyword_count: int
    notice_count: int
    deduped_count: int
    email_sent_count: int
    sheet_written_count: int
    error_message: Optional[str] = None
    executed_at: datetime


class ScraperConfig(BaseModel):
    enabled: bool = True
    notify_times: list[time] = Field(default_factory=lambda: [time(hour=9, minute=0)], min_length=1)
    gsheet_ids: list[str] = Field(default_factory=list)
    receiver_emails: list[EmailStr]
    keywords: list[str] = Field(min_length=1)
    excluded_keywords: list[str] = Field(default_factory=list)
    scheduler_status: Optional[SchedulerStatus] = None
    recent_runs: list[ScraperRunSummary] = Field(default_factory=list)

    @field_validator("keywords", "excluded_keywords", mode="before")
    @classmethod
    def normalize_keyword_values(cls, values):
        return normalize_keywords(values)


class ScraperDedupFilterRequest(BaseModel):
    run_id: str = Field(..., min_length=1, max_length=64)
    since_notified_at: Optional[datetime] = None
    notices: list[ScraperNotice] = Field(default_factory=list)


class ScraperDedupFilterResponse(BaseModel):
    run_id: str
    input_count: int
    kept_count: int
    filtered_count: int
    notices: list[ScraperNotice]


class ScraperRunReportRequest(BaseModel):
    run_id: str = Field(..., min_length=1, max_length=64)
    source: str = Field(default="cloud_run", min_length=1, max_length=20)
    status: Literal["success", "partial", "failed"] = "success"
    keyword_count: int = Field(default=0, ge=0)
    notice_count: int = Field(default=0, ge=0)
    deduped_count: int = Field(default=0, ge=0)
    email_sent_count: int = Field(default=0, ge=0)
    sheet_written_count: int = Field(default=0, ge=0)
    error_message: Optional[str] = Field(default=None, max_length=4000)
    executed_at: datetime = Field(default_factory=datetime.utcnow)
    notices: list[ScraperNotice] = Field(default_factory=list)


class ScraperRunReportResponse(BaseModel):
    success: bool
    message: str
    run_id: str


class TriggerScraperRequest(BaseModel):
    run_now: bool = True
    reason: Optional[str] = None


class TriggerScraperResponse(BaseModel):
    accepted: bool
    message: str
    task_id: str
