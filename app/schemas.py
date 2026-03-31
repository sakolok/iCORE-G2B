from datetime import datetime, time
from typing import Literal, Optional

from pydantic import BaseModel, EmailStr, Field


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
    hero_image_url: Optional[str] = None
    title_color: str = Field(default="#0f172a", pattern=r"^#[0-9a-fA-F]{6}$")
    subtitle_color: str = Field(default="#2563eb", pattern=r"^#[0-9a-fA-F]{6}$")
    body_color: str = Field(default="#334155", pattern=r"^#[0-9a-fA-F]{6}$")
    cta_text_color: str = Field(default="#ffffff", pattern=r"^#[0-9a-fA-F]{6}$")
    cta_bg_color: str = Field(default="#2563eb", pattern=r"^#[0-9a-fA-F]{6}$")
    background_color: str = Field(default="#f8fafc", pattern=r"^#[0-9a-fA-F]{6}$")


class LandingContent(BaseModel):
    title: str = Field(..., min_length=1, max_length=120)
    subtitle: str = Field(..., min_length=1, max_length=240)
    body: str = Field(..., min_length=1, max_length=1000)
    cta_text: str = Field(..., min_length=1, max_length=60)
    cta_url: str = Field(..., min_length=1, max_length=240)
    hero_image_url: Optional[str] = Field(default=None, max_length=500)
    hero_image_file_name: Optional[str] = Field(default=None, max_length=200)
    hero_image_mime_type: Optional[str] = Field(default=None, max_length=100)
    hero_image_base64: Optional[str] = None
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


class ScraperConfig(BaseModel):
    enabled: bool = True
    schedule_mode: Literal["daily", "interval"] = "daily"
    notify_time: time = time(hour=9, minute=0)
    interval_minutes: int = Field(default=60, ge=5, le=1440)
    dedup_mode: Literal["notice_id", "notice_id_and_title"] = "notice_id"
    dedup_retention_hours: int = Field(default=48, ge=1, le=720)
    receiver_emails: list[EmailStr]
    keywords: list[str]


class TriggerScraperRequest(BaseModel):
    run_now: bool = True
    reason: Optional[str] = None


class TriggerScraperResponse(BaseModel):
    accepted: bool
    message: str
    task_id: str
