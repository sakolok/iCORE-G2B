from sqlalchemy import inspect, select, text, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.core.config import settings
from app.data.models import (
    OrganizationMemberModel,
    OrganizationModel,
    OrganizationResultProfileModel,
    ScraperConfigModel,
    SystemMigrationModel,
    UserResultProfileModel,
    UserModel,
)
from app.g2b.opening_results.models import SheetDestinationModel
from app.services.auth_service import hash_password, verify_password

ORGANIZATION_MEMBERSHIP_BACKFILL_KEY = "2026-07-organization-membership-backfill"
USER_RESULT_PROFILE_BACKFILL_KEY = "2026-07-user-result-profile-backfill"
LEGACY_ORGANIZATION_SHEET_DESTINATIONS_DISABLED_KEY = (
    "2026-07-legacy-organization-sheet-destinations-disabled"
)
LEGACY_DEFAULT_ADMIN_PASSWORD = "icore1234!"


def _ensure_columns(
    engine: Engine,
    table_name: str,
    column_definitions: dict[str, str],
) -> None:
    inspector = inspect(engine)
    if table_name not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns(table_name)}
    for column_name, definition in column_definitions.items():
        if column_name in columns:
            continue
        try:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        f"ALTER TABLE {table_name} "
                        f"ADD COLUMN {column_name} {definition}"
                    )
                )
        except Exception:
            refreshed_columns = {
                column["name"]
                for column in inspect(engine).get_columns(table_name)
            }
            if column_name not in refreshed_columns:
                raise
        columns.add(column_name)


def _ensure_index(
    engine: Engine,
    table_name: str,
    index_name: str,
    columns: list[str],
) -> None:
    inspector = inspect(engine)
    if table_name not in inspector.get_table_names():
        return
    existing_indexes = {index["name"] for index in inspector.get_indexes(table_name)}
    if index_name in existing_indexes:
        return
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    f"CREATE INDEX {index_name} ON {table_name} "
                    f"({', '.join(columns)})"
                )
            )
    except Exception:
        refreshed_indexes = {
            index["name"] for index in inspect(engine).get_indexes(table_name)
        }
        if index_name not in refreshed_indexes:
            raise


def _ensure_unique_index(
    engine: Engine,
    table_name: str,
    index_name: str,
    columns: list[str],
) -> None:
    inspector = inspect(engine)
    if table_name not in inspector.get_table_names():
        return
    table_columns = {column["name"] for column in inspector.get_columns(table_name)}
    if not set(columns).issubset(table_columns):
        return
    desired_columns = tuple(columns)
    unique_columns = {
        tuple(constraint.get("column_names") or [])
        for constraint in inspector.get_unique_constraints(table_name)
    }
    unique_columns.update(
        tuple(index.get("column_names") or [])
        for index in inspector.get_indexes(table_name)
        if index.get("unique")
    )
    if desired_columns in unique_columns:
        return
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    f"CREATE UNIQUE INDEX {index_name} ON {table_name} "
                    f"({', '.join(columns)})"
                )
            )
    except Exception:
        refreshed = inspect(engine)
        refreshed_unique_columns = {
            tuple(constraint.get("column_names") or [])
            for constraint in refreshed.get_unique_constraints(table_name)
        }
        refreshed_unique_columns.update(
            tuple(index.get("column_names") or [])
            for index in refreshed.get_indexes(table_name)
            if index.get("unique")
        )
        if desired_columns not in refreshed_unique_columns:
            raise


def ensure_schema_compatibility(engine: Engine) -> None:
    _ensure_columns(
        engine,
        "users",
        {
            "email": "VARCHAR(320) NULL",
            "google_sub": "VARCHAR(255) NULL",
            "display_name": "VARCHAR(200) NULL",
            "last_login_at": "DATETIME NULL",
        },
    )
    _ensure_unique_index(
        engine,
        "users",
        "uq_users_email",
        ["email"],
    )
    _ensure_unique_index(
        engine,
        "users",
        "uq_users_google_sub",
        ["google_sub"],
    )
    _ensure_columns(
        engine,
        "scraper_configs",
        {"excluded_keywords": "TEXT NULL"},
    )
    _ensure_columns(
        engine,
        "scraper_notices",
        {
            "bid_notice_no": "VARCHAR(160) NULL",
            "bid_notice_ord": "VARCHAR(20) NULL",
            "business_name": "VARCHAR(500) NULL",
            "demand_agency_name": "VARCHAR(240) NULL",
            "base_amount": "NUMERIC(20, 2) NULL",
            "prearranged_price_decision_method": "VARCHAR(120) NULL",
            "proposal_deadline": "DATETIME NULL",
            "region_restriction": "VARCHAR(240) NULL",
            "region_restriction_api_status": "VARCHAR(20) NULL",
            "is_two_stage_bid": "BOOLEAN NULL",
        },
    )
    _ensure_index(
        engine,
        "scraper_notices",
        "ix_scraper_notices_bid_notice_no",
        ["bid_notice_no"],
    )
    _ensure_columns(
        engine,
        "sheet_destinations",
        {
            "export_lock_token": "VARCHAR(36) NULL",
            "export_lock_claimed_at": "DATETIME NULL",
        },
    )
    _ensure_columns(
        engine,
        "g2b_opening_collection_runs",
        {"claim_token": "VARCHAR(36) NULL"},
    )
    _ensure_columns(
        engine,
        "g2b_opening_rounds",
        {"entries_collected_at": "DATETIME NULL"},
    )
    _ensure_columns(
        engine,
        "g2b_pre_specification_sheet_exports",
        {
            "attempt_count": "INTEGER NOT NULL DEFAULT 1",
            "error_message": "TEXT NULL",
            "claimed_at": "DATETIME NULL",
            "succeeded_at": "DATETIME NULL",
            "created_at": "DATETIME NULL",
            "updated_at": "DATETIME NULL",
        },
    )
    _ensure_unique_index(
        engine,
        "sheet_destinations",
        "uq_sheet_destination_physical_target",
        ["spreadsheet_id", "tab_name"],
    )


def seed_defaults(db: Session) -> None:
    config = db.execute(select(ScraperConfigModel).limit(1)).scalar_one_or_none()
    if config is None:
        config = ScraperConfigModel(
            enabled=True,
            notify_times="09:00:00",
            gsheet_ids="",
            receiver_emails=settings.default_receiver_email,
            keywords="클라우드,AI,교육",
            excluded_keywords="",
        )
        db.add(config)

    organization = db.execute(
        select(OrganizationModel).where(
            OrganizationModel.slug == settings.default_organization_slug
        )
    ).scalar_one_or_none()
    if organization is None:
        organization = OrganizationModel(
            name=settings.default_organization_name,
            slug=settings.default_organization_slug,
            is_active=True,
        )
        db.add(organization)
        db.flush()

    admin = (
        db.execute(select(UserModel).where(UserModel.username == settings.default_admin_username))
        .scalar_one_or_none()
    )
    if admin is None:
        salt, password_hash = hash_password(settings.default_admin_password)
        admin = UserModel(
            username=settings.default_admin_username,
            password_salt=salt,
            password_hash=password_hash,
            email=settings.default_admin_email.strip().lower() or None,
            role="admin",
            is_active=True,
        )
        db.add(admin)
        db.flush()
    elif (
        settings.default_admin_password != LEGACY_DEFAULT_ADMIN_PASSWORD
        and verify_password(
            LEGACY_DEFAULT_ADMIN_PASSWORD,
            admin.password_salt,
            admin.password_hash,
        )
    ):
        admin.password_salt, admin.password_hash = hash_password(
            settings.default_admin_password
        )
    if admin.email is None and settings.default_admin_email.strip():
        admin.email = settings.default_admin_email.strip().lower()

    membership_backfill = db.get(
        SystemMigrationModel,
        ORGANIZATION_MEMBERSHIP_BACKFILL_KEY,
    )
    if membership_backfill is None:
        member_user_ids = set(db.scalars(select(OrganizationMemberModel.user_id)))
        for user in db.scalars(select(UserModel)):
            if user.id in member_user_ids:
                continue
            db.add(
                OrganizationMemberModel(
                    organization_id=organization.id,
                    user_id=user.id,
                    role="admin" if user.role == "admin" else "member",
                    is_active=True,
                )
            )
        db.add(SystemMigrationModel(key=ORGANIZATION_MEMBERSHIP_BACKFILL_KEY))

    profile = db.execute(
        select(OrganizationResultProfileModel).where(
            OrganizationResultProfileModel.organization_id == organization.id
        )
    ).scalar_one_or_none()
    if profile is None:
        profile = OrganizationResultProfileModel(
            organization_id=organization.id,
            enabled=config.enabled,
            keywords=config.keywords,
            excluded_keywords=config.excluded_keywords or "",
        )
        db.add(profile)
    db.flush()

    profile_backfill = db.get(
        SystemMigrationModel,
        USER_RESULT_PROFILE_BACKFILL_KEY,
    )
    if profile_backfill is None:
        existing_user_profile_ids = set(
            db.scalars(select(UserResultProfileModel.user_id))
        )
        active_memberships = db.scalars(
            select(OrganizationMemberModel).where(
                OrganizationMemberModel.is_active.is_(True)
            )
        )
        organization_profiles = {
            item.organization_id: item
            for item in db.scalars(select(OrganizationResultProfileModel))
        }
        for membership in active_memberships:
            if membership.user_id in existing_user_profile_ids:
                continue
            source_profile = organization_profiles.get(membership.organization_id)
            db.add(
                UserResultProfileModel(
                    organization_id=membership.organization_id,
                    user_id=membership.user_id,
                    enabled=source_profile.enabled if source_profile else False,
                    keywords=source_profile.keywords if source_profile else "",
                    excluded_keywords=(
                        source_profile.excluded_keywords if source_profile else ""
                    ),
                )
            )
        db.add(SystemMigrationModel(key=USER_RESULT_PROFILE_BACKFILL_KEY))
        db.flush()

    legacy_destinations = db.get(
        SystemMigrationModel,
        LEGACY_ORGANIZATION_SHEET_DESTINATIONS_DISABLED_KEY,
    )
    if legacy_destinations is None:
        db.execute(
            update(SheetDestinationModel)
            .where(SheetDestinationModel.owner_user_id.is_(None))
            .values(is_active=False, is_default=False)
        )
        db.add(
            SystemMigrationModel(
                key=LEGACY_ORGANIZATION_SHEET_DESTINATIONS_DISABLED_KEY
            )
        )

    from app.g2b.opening_results.matching import (
        sync_organization_matches,
        sync_user_matches,
    )

    sync_organization_matches(db, organization_id=organization.id)
    sync_user_matches(db, organization_id=organization.id)
    db.commit()
