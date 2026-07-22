from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class SystemMigrationModel(Base):
    __tablename__ = "system_migrations"

    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    applied_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


class ScraperConfigModel(Base):
    __tablename__ = "scraper_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    notify_times: Mapped[str] = mapped_column(Text, nullable=False, default="09:00:00")
    gsheet_ids: Mapped[str] = mapped_column(Text, nullable=False, default="")
    receiver_emails: Mapped[str] = mapped_column(Text, nullable=False)
    keywords: Mapped[str] = mapped_column(Text, nullable=False)
    excluded_keywords: Mapped[str | None] = mapped_column(Text, nullable=True, default="")
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
    # Canonical bid-notice fields used by the opening-result Sheet export.
    # The bid-notice module owns population of these fields; opening results are read-only.
    bid_notice_no: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)
    bid_notice_ord: Mapped[str | None] = mapped_column(String(20), nullable=True)
    business_name: Mapped[str | None] = mapped_column(String(500), nullable=True)
    demand_agency_name: Mapped[str | None] = mapped_column(String(240), nullable=True)
    base_amount: Mapped[Decimal | None] = mapped_column(Numeric(20, 2), nullable=True)
    prearranged_price_decision_method: Mapped[str | None] = mapped_column(
        String(120),
        nullable=True,
    )
    proposal_deadline: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    region_restriction: Mapped[str | None] = mapped_column(String(240), nullable=True)
    region_restriction_api_status: Mapped[str | None] = mapped_column(
        String(20), nullable=True
    )
    is_two_stage_bid: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)


class UserModel(Base):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("email", name="uq_users_email"),
        UniqueConstraint("google_sub", name="uq_users_google_sub"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    password_salt: Mapped[str] = mapped_column(String(64), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    google_sub: Mapped[str | None] = mapped_column(String(255), nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
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


class OrganizationModel(Base):
    __tablename__ = "organizations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    slug: Mapped[str] = mapped_column(String(120), nullable=False, unique=True, index=True)
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


class OrganizationMemberModel(Base):
    __tablename__ = "organization_members"
    __table_args__ = (
        UniqueConstraint("organization_id", "user_id", name="uq_organization_member"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True, index=True
    )
    role: Mapped[str] = mapped_column(String(30), nullable=False, default="member")
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


class OrganizationResultProfileModel(Base):
    __tablename__ = "organization_result_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    keywords: Mapped[str] = mapped_column(Text, nullable=False)
    excluded_keywords: Mapped[str] = mapped_column(Text, nullable=False, default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class UserResultProfileModel(Base):
    __tablename__ = "user_result_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True, index=True
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    keywords: Mapped[str] = mapped_column(Text, nullable=False)
    excluded_keywords: Mapped[str] = mapped_column(Text, nullable=False, default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
