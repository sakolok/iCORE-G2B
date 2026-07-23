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
