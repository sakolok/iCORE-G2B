from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class LandingTemplateModel(Base):
    __tablename__ = "landing_templates"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(String(240), nullable=False)
    preview_style: Mapped[str] = mapped_column(String(120), nullable=False)


class LandingPageModel(Base):
    __tablename__ = "landing_pages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    template_id: Mapped[str] = mapped_column(String(80), nullable=False)
    business_topic: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    business_name: Mapped[str] = mapped_column(String(120), nullable=False)
    major_categories: Mapped[str] = mapped_column(Text, nullable=False, default="")
    minor_categories: Mapped[str] = mapped_column(Text, nullable=False, default="")
    slug: Mapped[str] = mapped_column(String(120), nullable=False, unique=True, index=True)
    url: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    retention_days: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    is_visible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    custom_domain: Mapped[str | None] = mapped_column(String(240), nullable=True)
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    subtitle: Mapped[str] = mapped_column(String(240), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    cta_text: Mapped[str] = mapped_column(String(60), nullable=False)
    cta_url: Mapped[str] = mapped_column(String(240), nullable=False)
    hero_image_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    primary_color: Mapped[str] = mapped_column(String(7), nullable=False)
    secondary_color: Mapped[str] = mapped_column(String(7), nullable=False)
    background_color: Mapped[str] = mapped_column(String(7), nullable=False)
    features_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    curriculum_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    target_audience_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    deployed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class ScraperConfigModel(Base):
    __tablename__ = "scraper_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    notify_times: Mapped[str] = mapped_column(Text, nullable=False, default="09:00:00")
    gsheet_ids: Mapped[str] = mapped_column(Text, nullable=False, default="")
    receiver_emails: Mapped[str] = mapped_column(Text, nullable=False)
    keywords: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class ScraperRunModel(Base):
    __tablename__ = "scraper_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="cloud_run")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="success")
    keyword_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    notice_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    deduped_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    email_sent_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sheet_written_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    executed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


class ScraperNoticeModel(Base):
    __tablename__ = "scraper_notices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    dedup_key: Mapped[str] = mapped_column(String(190), nullable=False, unique=True, index=True)
    notice_id: Mapped[str] = mapped_column(String(160), nullable=False, default="")
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    agency: Mapped[str | None] = mapped_column(String(240), nullable=True)
    estimated_price: Mapped[str | None] = mapped_column(String(120), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notice_url: Mapped[str | None] = mapped_column(String(600), nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)


class UserModel(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    password_salt: Mapped[str] = mapped_column(String(64), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    role: Mapped[str] = mapped_column(String(30), nullable=False, default="admin")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
