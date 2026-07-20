from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.data.models import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class BidOpeningRoundModel(Base):
    __tablename__ = "g2b_opening_rounds"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    external_key: Mapped[str] = mapped_column(String(240), nullable=False, unique=True, index=True)
    business_type: Mapped[str] = mapped_column(String(20), nullable=False, default="SERVICE")
    bid_notice_no: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    bid_notice_ord: Mapped[str] = mapped_column(String(20), nullable=False, default="00")
    bid_class_no: Mapped[str] = mapped_column(String(40), nullable=False, default="0")
    rebid_no: Mapped[str] = mapped_column(String(20), nullable=False, default="0")
    title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="UNKNOWN", index=True)
    status_label: Mapped[str | None] = mapped_column(String(120), nullable=True)
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    participant_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    opening_notice: Mapped[str | None] = mapped_column(Text, nullable=True)
    notice_agency_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    notice_agency_name: Mapped[str | None] = mapped_column(String(240), nullable=True)
    demand_agency_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    demand_agency_name: Mapped[str | None] = mapped_column(String(240), nullable=True)
    winner_business_no: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    winner_company_name: Mapped[str | None] = mapped_column(String(240), nullable=True, index=True)
    winning_amount: Mapped[Decimal | None] = mapped_column(Numeric(20, 2), nullable=True)
    winning_rate: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    final_awarded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    entries_collected_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )


class BidOpeningEntryModel(Base):
    __tablename__ = "g2b_opening_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    round_id: Mapped[int] = mapped_column(
        ForeignKey("g2b_opening_rounds.id", ondelete="CASCADE"), nullable=False, index=True
    )
    external_key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    business_no: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    company_name: Mapped[str | None] = mapped_column(String(240), nullable=True, index=True)
    ceo_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    bid_amount: Mapped[Decimal | None] = mapped_column(Numeric(20, 2), nullable=True)
    bid_rate: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    bid_price_score: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    technical_raw_score: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    technical_score: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    total_score: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    official_total_score: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    bid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    draw_number_1: Mapped[str | None] = mapped_column(String(40), nullable=True)
    draw_number_2: Mapped[str | None] = mapped_column(String(40), nullable=True)
    is_winner: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )


class BidResultSnapshotModel(Base):
    __tablename__ = "g2b_result_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "entity_type", "entity_key", "payload_hash", name="uq_g2b_result_snapshot_version"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    entity_type: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    entity_key: Mapped[str] = mapped_column(String(240), nullable=False, index=True)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    raw_payload: Mapped[str] = mapped_column(Text, nullable=False)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)


class BidOpeningCollectionRunModel(Base):
    __tablename__ = "g2b_opening_collection_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_key: Mapped[str] = mapped_column(String(80), nullable=False, unique=True, index=True)
    business_type: Mapped[str] = mapped_column(String(20), nullable=False)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="RUNNING")
    claim_token: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    fetched_round_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    fetched_entry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    inserted_round_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_round_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    inserted_entry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_entry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    skipped_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class BidOpeningCollectionLeaseModel(Base):
    __tablename__ = "g2b_opening_collection_leases"

    business_type: Mapped[str] = mapped_column(String(20), primary_key=True)
    claim_token: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class OrganizationOpeningResultMatchModel(Base):
    __tablename__ = "organization_opening_result_matches"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "result_external_key",
            name="uq_organization_opening_result_match",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    round_id: Mapped[int] = mapped_column(
        ForeignKey("g2b_opening_rounds.id", ondelete="CASCADE"), nullable=False, index=True
    )
    result_external_key: Mapped[str] = mapped_column(String(240), nullable=False, index=True)
    matched_keywords: Mapped[str] = mapped_column(Text, nullable=False, default="")
    is_current_match: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    first_matched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )


class UserOpeningResultMatchModel(Base):
    __tablename__ = "user_opening_result_matches"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "result_external_key",
            name="uq_user_opening_result_match",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    round_id: Mapped[int] = mapped_column(
        ForeignKey("g2b_opening_rounds.id", ondelete="CASCADE"), nullable=False, index=True
    )
    result_external_key: Mapped[str] = mapped_column(String(240), nullable=False, index=True)
    matched_keywords: Mapped[str] = mapped_column(Text, nullable=False, default="")
    is_current_match: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    first_matched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )


class UserOpeningResultStateModel(Base):
    __tablename__ = "user_opening_result_states"
    __table_args__ = (
        UniqueConstraint("user_id", "result_external_key", name="uq_user_opening_result_state"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    result_external_key: Mapped[str] = mapped_column(String(240), nullable=False, index=True)
    state: Mapped[str] = mapped_column(String(20), nullable=False)
    acted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class SheetDestinationModel(Base):
    __tablename__ = "sheet_destinations"
    __table_args__ = (
        UniqueConstraint(
            "spreadsheet_id",
            "tab_name",
            name="uq_sheet_destination_physical_target",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    owner_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    label: Mapped[str] = mapped_column(String(120), nullable=False)
    spreadsheet_id: Mapped[str] = mapped_column(String(240), nullable=False)
    tab_name: Mapped[str] = mapped_column(String(120), nullable=False, default="개찰결과")
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    export_lock_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    export_lock_claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )


class SheetExportModel(Base):
    __tablename__ = "sheet_exports"
    __table_args__ = (
        UniqueConstraint(
            "destination_id",
            "result_external_key",
            name="uq_sheet_export_destination_result",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    destination_id: Mapped[int] = mapped_column(
        ForeignKey("sheet_destinations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    result_external_key: Mapped[str] = mapped_column(String(240), nullable=False, index=True)
    exported_by_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="PENDING", index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    claimed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    succeeded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )
