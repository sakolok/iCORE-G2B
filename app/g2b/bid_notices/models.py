from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.data.models import Base
from app.g2b.opening_results.models import SheetDestinationModel  # noqa: F401


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class BidNoticeCollectionRunModel(Base):
    __tablename__ = "g2b_bid_notice_collection_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_key: Mapped[str] = mapped_column(String(100), nullable=False, unique=True, index=True)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="RUNNING")
    fetched_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    inserted_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class BidNoticeDocumentAnalysisModel(Base):
    __tablename__ = "g2b_bid_notice_document_analyses"
    __table_args__ = (
        UniqueConstraint(
            "notice_id",
            "attachment_key",
            "analyzer_version",
            name="uq_bid_notice_document_analysis",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    notice_id: Mapped[int] = mapped_column(
        ForeignKey("scraper_notices.id", ondelete="CASCADE"), nullable=False, index=True
    )
    attachment_key: Mapped[str] = mapped_column(String(64), nullable=False)
    attachment_name: Mapped[str] = mapped_column(String(500), nullable=False)
    attachment_url: Mapped[str] = mapped_column(Text, nullable=False)
    analyzer_version: Mapped[str] = mapped_column(String(40), nullable=False)
    content_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="PENDING")
    needs_region: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    needs_industry: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    region_result: Mapped[str | None] = mapped_column(Text, nullable=True)
    region_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    industry_codes: Mapped[str | None] = mapped_column(Text, nullable=True)
    industry_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    evidence: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    analyzed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )


class UserBidNoticeProfileModel(Base):
    __tablename__ = "user_bid_notice_profiles"
    __table_args__ = (UniqueConstraint("user_id", name="uq_user_bid_notice_profile"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    keywords: Mapped[str] = mapped_column(Text, nullable=False, default="")
    excluded_keywords: Mapped[str] = mapped_column(Text, nullable=False, default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )


class UserBidNoticeMatchModel(Base):
    __tablename__ = "user_bid_notice_matches"
    __table_args__ = (
        UniqueConstraint("user_id", "notice_id", name="uq_user_bid_notice_match"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    notice_id: Mapped[int] = mapped_column(
        ForeignKey("scraper_notices.id", ondelete="CASCADE"), nullable=False, index=True
    )
    matched_keyword: Mapped[str | None] = mapped_column(String(200), nullable=True)
    is_current_match: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    matched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)


class UserBidNoticeStateModel(Base):
    __tablename__ = "user_bid_notice_states"
    __table_args__ = (
        UniqueConstraint("user_id", "notice_id", name="uq_user_bid_notice_state"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    notice_id: Mapped[int] = mapped_column(
        ForeignKey("scraper_notices.id", ondelete="CASCADE"), nullable=False, index=True
    )
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="DISMISSED")
    acted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)


class BidNoticeSheetExportModel(Base):
    __tablename__ = "g2b_bid_notice_sheet_exports"
    __table_args__ = (
        UniqueConstraint(
            "destination_id", "notice_id", name="uq_bid_notice_sheet_export"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    destination_id: Mapped[int] = mapped_column(
        ForeignKey("sheet_destinations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    notice_id: Mapped[int] = mapped_column(
        ForeignKey("scraper_notices.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="PENDING")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    succeeded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )
