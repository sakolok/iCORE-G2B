from datetime import datetime, time
from typing import Literal, Optional

from pydantic import BaseModel, EmailStr, Field


class StatItem(BaseModel):
    title: str = Field(..., max_length=100)
    value: str = Field(..., max_length=100)

class InfoItem(BaseModel):
    label: str = Field(..., max_length=100)
    val: str = Field(..., max_length=200)

class FaqItem(BaseModel):
    q: str = Field(..., max_length=300)
    a: str = Field(..., max_length=1000)

class FeatureItem(BaseModel):
    title: str = Field(..., max_length=100)
    description: str = Field(..., max_length=500)
    image_base64: Optional[str] = None
    image_url: Optional[str] = Field(default=None, max_length=500)

class CurriculumItem(BaseModel):
    step: str = Field(..., max_length=50)
    title: str = Field(..., max_length=100)
    description: str = Field(..., max_length=500)
    image_base64: Optional[str] = None
    image_url: Optional[str] = Field(default=None, max_length=500)

class TargetAudienceItem(BaseModel):
    description: str = Field(..., max_length=200)

class LandingTemplate(BaseModel):
    id: str
    name: str
    description: str
    preview_style: str


class LandingTemplateDetail(BaseModel):
    id: str
    name: str
    description: str
    preview_style: str
    title: str
    subtitle: str
    body: str
    cta_text: str
    features: list[dict] = Field(default_factory=list)
    curriculum: list[dict] = Field(default_factory=list)
    target_audience: list[dict] = Field(default_factory=list)
    stats: list[dict] = Field(default_factory=list)
    infos: list[dict] = Field(default_factory=list)
    faqs: list[dict] = Field(default_factory=list)
    cta_text_color: str = Field(default="#ffffff", pattern=r"^#[0-9a-fA-F]{6}$")
    cta_bg_color: str = Field(default="#2563eb", pattern=r"^#[0-9a-fA-F]{6}$")
    background_color: str = Field(default="#f8fafc", pattern=r"^#[0-9a-fA-F]{6}$")


class LandingContent(BaseModel):
    title: str = Field(..., min_length=1, max_length=120)
    subtitle: str = Field(..., min_length=1, max_length=240)
    body: str = Field(..., min_length=1, max_length=1000)
    cta_text: str = Field(..., min_length=1, max_length=60)
    cta_url: str = Field(..., min_length=1, max_length=240)
    features: list[FeatureItem] = Field(default_factory=list)
    curriculum: list[CurriculumItem] = Field(default_factory=list)
    target_audience: list[TargetAudienceItem] = Field(default_factory=list)
    stats: list[StatItem] = Field(default_factory=list)
    infos: list[InfoItem] = Field(default_factory=list)
    faqs: list[FaqItem] = Field(default_factory=list)
    primary_color: str = Field(default="#2563eb", pattern=r"^#[0-9a-fA-F]{6}$")
    secondary_color: str = Field(default="#0f172a", pattern=r"^#[0-9a-fA-F]{6}$")
    background_color: str = Field(default="#f8fafc", pattern=r"^#[0-9a-fA-F]{6}$")


class DeployRequest(BaseModel):
    template_id: str
    business_topic: str = Field(..., min_length=1, max_length=120)
    business_name: str = Field(..., min_length=1, max_length=120)
    major_categories: list[str] = Field(default_factory=list)
    minor_categories: list[str] = Field(default_factory=list)
    slug: str = Field(..., pattern=r"^[a-z0-9\-]+$")
    custom_domain: Optional[str] = None
    retention_days: int = Field(default=30, ge=1, le=3650)
    content: LandingContent


class DeployResponse(BaseModel):
    deployment_id: str
    landing_page_id: str
    target_path: str
    public_url: str
    cdn_enabled: bool
    message: str


class LandingPage(BaseModel):
    id: str
    template_id: str
    business_topic: str
    business_name: str
    major_categories: list[str]
    minor_categories: list[str]
    slug: str
    url: str
    status: Literal["active", "paused", "archived"]
    retention_days: int
    expires_at: datetime
    is_visible: bool
    created_at: datetime
    updated_at: datetime


class UpdateLandingPageRequest(BaseModel):
    business_topic: str = Field(..., min_length=1, max_length=120)
    business_name: str = Field(..., min_length=1, max_length=120)
    major_categories: list[str] = Field(default_factory=list)
    minor_categories: list[str] = Field(default_factory=list)
    status: Literal["active", "paused", "archived"]


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=100)
    password: str = Field(..., min_length=1, max_length=200)


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "Bearer"
    username: str
    role: str


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
    scheduler_status: Optional[SchedulerStatus] = None
    recent_runs: list[ScraperRunSummary] = Field(default_factory=list)


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
