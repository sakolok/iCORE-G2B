from datetime import time
from typing import Optional

from pydantic import BaseModel, EmailStr, Field


class LandingTemplate(BaseModel):
    id: str
    name: str
    description: str
    preview_style: str


class LandingContent(BaseModel):
    title: str = Field(..., min_length=1, max_length=120)
    subtitle: str = Field(..., min_length=1, max_length=240)
    body: str = Field(..., min_length=1, max_length=1000)
    cta_text: str = Field(..., min_length=1, max_length=60)
    cta_url: str = Field(..., min_length=1, max_length=240)


class DeployRequest(BaseModel):
    template_id: str
    business_topic: str
    business_name: str
    slug: str = Field(..., pattern=r"^[a-z0-9\-]+$")
    custom_domain: Optional[str] = None
    content: LandingContent


class DeployResponse(BaseModel):
    deployment_id: str
    target_path: str
    public_url: str
    cdn_enabled: bool
    message: str


class BusinessSite(BaseModel):
    id: str
    topic: str
    name: str
    url: str
    status: str


class CreateBusinessSiteRequest(BaseModel):
    topic: str
    name: str
    url: str
    status: str = "active"


class ScraperConfig(BaseModel):
    enabled: bool = True
    notify_time: time
    receiver_email: EmailStr
    keywords: list[str]


class TriggerScraperRequest(BaseModel):
    run_now: bool = True
    reason: Optional[str] = None


class TriggerScraperResponse(BaseModel):
    accepted: bool
    message: str
