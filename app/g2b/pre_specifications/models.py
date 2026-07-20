from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.data.models import Base
# Registers the shared Sheet destination table before this module's foreign key is built.
from app.g2b.opening_results.models import SheetDestinationModel  # noqa: F401


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PreSpecificationModel(Base):
    __tablename__ = "g2b_pre_specifications"

    bf_spec_rgst_no: Mapped[str] = mapped_column(String(80), primary_key=True)
    bid_notice_no: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)
    bid_notice_ord: Mapped[str | None] = mapped_column(String(20), nullable=True)
    reference_no: Mapped[str | None] = mapped_column(String(160), nullable=True)
    business_name: Mapped[str | None] = mapped_column(String(500), nullable=True, index=True)
    business_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    demand_agency_name: Mapped[str | None] = mapped_column(String(240), nullable=True, index=True)
    ordering_agency_name: Mapped[str | None] = mapped_column(String(240), nullable=True)
    allocated_budget: Mapped[Decimal | None] = mapped_column(Numeric(20, 2), nullable=True)
    registered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    opinion_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    delivery_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    contact_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    contact_phone: Mapped[str | None] = mapped_column(String(80), nullable=True)
    attachments_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    raw_payload: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow)


class PreSpecificationSnapshotModel(Base):
    __tablename__ = "g2b_pre_specification_snapshots"
    __table_args__ = (UniqueConstraint("bf_spec_rgst_no", "payload_hash", name="uq_pre_spec_snapshot_version"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    bf_spec_rgst_no: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    raw_payload: Mapped[str] = mapped_column(Text, nullable=False)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)


class PreSpecificationCollectionRunModel(Base):
    __tablename__ = "g2b_pre_specification_collection_runs"

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


class UserPreSpecificationStateModel(Base):
    __tablename__ = "user_pre_specification_states"
    __table_args__ = (UniqueConstraint("user_id", "bf_spec_rgst_no", name="uq_user_pre_specification_state"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    bf_spec_rgst_no: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    state: Mapped[str] = mapped_column(String(20), nullable=False)
    acted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)


class PreSpecificationSheetExportModel(Base):
    __tablename__ = "g2b_pre_specification_sheet_exports"
    __table_args__ = (UniqueConstraint("destination_id", "bf_spec_rgst_no", name="uq_pre_spec_sheet_export"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    destination_id: Mapped[int] = mapped_column(ForeignKey("sheet_destinations.id", ondelete="CASCADE"), nullable=False, index=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    bf_spec_rgst_no: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    exported_by_user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="PENDING")
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    succeeded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
