from datetime import datetime, time, timezone

from sqlalchemy import Boolean, DateTime, Integer, String, Text, Time
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
    slug: Mapped[str] = mapped_column(String(120), nullable=False, unique=True, index=True)
    url: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
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
    schedule_mode: Mapped[str] = mapped_column(String(20), nullable=False, default="daily")
    notify_time: Mapped[time] = mapped_column(Time, nullable=False, default=time(hour=9, minute=0))
    interval_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=60)
    dedup_mode: Mapped[str] = mapped_column(String(40), nullable=False, default="notice_id")
    dedup_retention_hours: Mapped[int] = mapped_column(Integer, nullable=False, default=48)
    receiver_emails: Mapped[str] = mapped_column(Text, nullable=False)
    keywords: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
