import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
from uuid import uuid4

from sqlalchemy import and_, exists, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, aliased

from app.data.models import (
    OrganizationMemberModel,
    OrganizationResultProfileModel,
    UserModel,
    UserResultProfileModel,
)
from app.g2b.keyword_policy import evaluate_keyword_title, normalize_keywords
from app.g2b.opening_results.models import (
    BidOpeningRoundModel,
    OrganizationOpeningResultMatchModel,
    SheetDestinationModel,
    SheetExportModel,
    UserOpeningResultMatchModel,
    UserOpeningResultStateModel,
)


MATCH_LOOKBACK_DAYS = 14
ARCHIVE_RETENTION_DAYS = 14
EXPORT_CLAIM_MINUTES = 15
USER_TERMINAL_STATES = ("EXPORTED", "DISMISSED")


class ResultAccessError(LookupError):
    pass


class SheetDestinationAccessError(LookupError):
    pass


class SheetDestinationConflictError(RuntimeError):
    pass


class SheetExportConflictError(RuntimeError):
    pass


def normalize_spreadsheet_id(value: str) -> str:
    text = value.strip()
    if not text:
        raise ValueError("Google Sheet ID 또는 URL을 입력하세요.")
    if "://" in text:
        parsed = urlparse(text)
        if parsed.scheme not in {"http", "https"} or parsed.netloc != "docs.google.com":
            raise ValueError("Google Sheets URL 또는 Spreadsheet ID만 사용할 수 있습니다.")
        parts = [part for part in parsed.path.split("/") if part]
        try:
            text = parts[parts.index("d") + 1]
        except (ValueError, IndexError) as error:
            raise ValueError("Google Sheets URL에서 Spreadsheet ID를 찾을 수 없습니다.") from error
    if not re.fullmatch(r"[A-Za-z0-9_-]+", text):
        raise ValueError("올바른 Google Spreadsheet ID가 아닙니다.")
    return text


@dataclass(frozen=True)
class SheetExportClaimBatch:
    destination_id: int
    lock_token: str
    records: list[SheetExportModel]


@dataclass(frozen=True)
class ArchivedOpeningResult:
    row: BidOpeningRoundModel
    handled_state: str
    handled_at: datetime
    can_restore: bool


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    return (
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None
        else value.astimezone(timezone.utc)
    )


def _split_keywords(value: str | None) -> list[str]:
    return normalize_keywords(value)


def sync_organization_matches(
    db: Session,
    *,
    organization_id: int | None = None,
    now: datetime | None = None,
) -> int:
    profiles_statement = select(OrganizationResultProfileModel)
    if organization_id is not None:
        profiles_statement = profiles_statement.where(
            OrganizationResultProfileModel.organization_id == organization_id
        )
    profiles = (
        db.execute(
            profiles_statement.order_by(OrganizationResultProfileModel.id).with_for_update()
        )
        .scalars()
        .all()
    )
    if not profiles:
        return 0

    current = now or _utcnow()
    cutoff = current - timedelta(days=MATCH_LOOKBACK_DAYS)
    rounds = (
        db.execute(
            select(BidOpeningRoundModel).where(
                or_(
                    BidOpeningRoundModel.opened_at >= cutoff,
                    BidOpeningRoundModel.collected_at >= cutoff,
                )
            )
        )
        .scalars()
        .all()
    )
    changed_count = 0
    for profile in profiles:
        existing_matches = (
            db.execute(
                select(OrganizationOpeningResultMatchModel).where(
                    OrganizationOpeningResultMatchModel.organization_id
                    == profile.organization_id
                )
            )
            .scalars()
            .all()
        )
        existing_by_key = {row.result_external_key: row for row in existing_matches}
        current_keys: set[str] = set()
        keywords = _split_keywords(profile.keywords)
        excluded_keywords = _split_keywords(profile.excluded_keywords)

        if profile.enabled and keywords:
            for round_row in rounds:
                decision = evaluate_keyword_title(
                    round_row.title,
                    keywords,
                    excluded_keywords,
                )
                if not decision.keep:
                    continue
                current_keys.add(round_row.external_key)
                match = existing_by_key.get(round_row.external_key)
                if match is None:
                    round_row.entries_collected_at = None
                    db.add(
                        OrganizationOpeningResultMatchModel(
                            organization_id=profile.organization_id,
                            round_id=round_row.id,
                            result_external_key=round_row.external_key,
                            matched_keywords=decision.matched_keyword,
                            is_current_match=True,
                        )
                    )
                    changed_count += 1
                    continue
                changed = (
                    match.round_id != round_row.id
                    or not match.is_current_match
                    or match.matched_keywords != decision.matched_keyword
                )
                if not match.is_current_match:
                    round_row.entries_collected_at = None
                match.round_id = round_row.id
                match.is_current_match = True
                match.matched_keywords = decision.matched_keyword
                if changed:
                    changed_count += 1

        for match in existing_matches:
            if match.result_external_key in current_keys or not match.is_current_match:
                continue
            match.is_current_match = False
            changed_count += 1

    db.flush()
    return changed_count


def get_result_profile(
    db: Session,
    organization_id: int,
    *,
    lock_for_update: bool = False,
) -> OrganizationResultProfileModel:
    statement = select(OrganizationResultProfileModel).where(
        OrganizationResultProfileModel.organization_id == organization_id
    )
    if lock_for_update:
        statement = statement.with_for_update()
    profile = db.execute(statement).scalar_one_or_none()
    if profile is None:
        profile = OrganizationResultProfileModel(
            organization_id=organization_id,
            enabled=True,
            keywords="",
            excluded_keywords="",
        )
        db.add(profile)
        db.flush()
    return profile


def update_result_profile(
    db: Session,
    *,
    organization_id: int,
    enabled: bool,
    keywords: list[str],
    excluded_keywords: list[str],
) -> OrganizationResultProfileModel:
    profile = get_result_profile(db, organization_id, lock_for_update=True)
    profile.enabled = enabled
    profile.keywords = ",".join(normalize_keywords(keywords))
    profile.excluded_keywords = ",".join(normalize_keywords(excluded_keywords))
    profile.updated_at = _utcnow()
    sync_organization_matches(db, organization_id=organization_id)
    db.commit()
    db.refresh(profile)
    return profile


def _ensure_user_result_profiles(db: Session) -> None:
    existing_user_ids = set(db.scalars(select(UserResultProfileModel.user_id)))
    memberships = db.execute(
        select(OrganizationMemberModel, UserModel)
        .join(UserModel, UserModel.id == OrganizationMemberModel.user_id)
        .where(
            OrganizationMemberModel.is_active.is_(True),
            UserModel.is_active.is_(True),
        )
    ).all()
    for membership, user in memberships:
        if user.id in existing_user_ids:
            continue
        db.add(
            UserResultProfileModel(
                organization_id=membership.organization_id,
                user_id=user.id,
                enabled=False,
                keywords="",
                excluded_keywords="",
            )
        )
        existing_user_ids.add(user.id)
    db.flush()


def get_user_result_profile(
    db: Session,
    *,
    organization_id: int,
    user_id: int,
    lock_for_update: bool = False,
) -> UserResultProfileModel:
    statement = select(UserResultProfileModel).where(
        UserResultProfileModel.organization_id == organization_id,
        UserResultProfileModel.user_id == user_id,
    )
    if lock_for_update:
        statement = statement.with_for_update()
    profile = db.execute(statement).scalar_one_or_none()
    if profile is None:
        profile = UserResultProfileModel(
            organization_id=organization_id,
            user_id=user_id,
            enabled=False,
            keywords="",
            excluded_keywords="",
        )
        db.add(profile)
        db.flush()
    return profile


def sync_user_matches(
    db: Session,
    *,
    organization_id: int | None = None,
    user_id: int | None = None,
    now: datetime | None = None,
) -> int:
    _ensure_user_result_profiles(db)
    profiles_statement = (
        select(UserResultProfileModel)
        .join(UserModel, UserModel.id == UserResultProfileModel.user_id)
        .join(
            OrganizationMemberModel,
            and_(
                OrganizationMemberModel.user_id == UserResultProfileModel.user_id,
                OrganizationMemberModel.organization_id
                == UserResultProfileModel.organization_id,
            ),
        )
        .where(
            UserModel.is_active.is_(True),
            OrganizationMemberModel.is_active.is_(True),
        )
    )
    if organization_id is not None:
        profiles_statement = profiles_statement.where(
            UserResultProfileModel.organization_id == organization_id
        )
    if user_id is not None:
        profiles_statement = profiles_statement.where(
            UserResultProfileModel.user_id == user_id
        )
    profiles = (
        db.execute(
            profiles_statement.order_by(UserResultProfileModel.id).with_for_update()
        )
        .scalars()
        .all()
    )
    if not profiles:
        return 0

    current = now or _utcnow()
    cutoff = current - timedelta(days=MATCH_LOOKBACK_DAYS)
    rounds = (
        db.execute(
            select(BidOpeningRoundModel).where(
                or_(
                    BidOpeningRoundModel.opened_at >= cutoff,
                    BidOpeningRoundModel.collected_at >= cutoff,
                )
            )
        )
        .scalars()
        .all()
    )
    changed_count = 0
    for profile in profiles:
        existing_matches = (
            db.execute(
                select(UserOpeningResultMatchModel).where(
                    UserOpeningResultMatchModel.user_id == profile.user_id
                )
            )
            .scalars()
            .all()
        )
        existing_by_key = {row.result_external_key: row for row in existing_matches}
        current_keys: set[str] = set()
        keywords = _split_keywords(profile.keywords)
        excluded_keywords = _split_keywords(profile.excluded_keywords)

        if profile.enabled and keywords:
            for round_row in rounds:
                decision = evaluate_keyword_title(
                    round_row.title,
                    keywords,
                    excluded_keywords,
                )
                if not decision.keep:
                    continue
                current_keys.add(round_row.external_key)
                match = existing_by_key.get(round_row.external_key)
                if match is None:
                    db.add(
                        UserOpeningResultMatchModel(
                            organization_id=profile.organization_id,
                            user_id=profile.user_id,
                            round_id=round_row.id,
                            result_external_key=round_row.external_key,
                            matched_keywords=decision.matched_keyword,
                            is_current_match=True,
                        )
                    )
                    changed_count += 1
                    continue
                changed = (
                    match.organization_id != profile.organization_id
                    or match.round_id != round_row.id
                    or not match.is_current_match
                    or match.matched_keywords != decision.matched_keyword
                )
                match.organization_id = profile.organization_id
                match.round_id = round_row.id
                match.is_current_match = True
                match.matched_keywords = decision.matched_keyword
                if changed:
                    changed_count += 1

        for match in existing_matches:
            if match.result_external_key in current_keys or not match.is_current_match:
                continue
            match.is_current_match = False
            changed_count += 1

    db.flush()
    return changed_count


def update_user_result_profile(
    db: Session,
    *,
    organization_id: int,
    user_id: int,
    enabled: bool,
    keywords: list[str],
    excluded_keywords: list[str],
) -> UserResultProfileModel:
    profile = get_user_result_profile(
        db,
        organization_id=organization_id,
        user_id=user_id,
        lock_for_update=True,
    )
    profile.enabled = enabled
    profile.keywords = ",".join(normalize_keywords(keywords))
    profile.excluded_keywords = ",".join(normalize_keywords(excluded_keywords))
    profile.updated_at = _utcnow()
    sync_user_matches(
        db,
        organization_id=organization_id,
        user_id=user_id,
    )
    db.commit()
    db.refresh(profile)
    return profile


def visible_result_predicates(organization_id: int, user_id: int) -> tuple:
    user_handled = exists(
        select(UserOpeningResultStateModel.id).where(
            UserOpeningResultStateModel.organization_id == organization_id,
            UserOpeningResultStateModel.user_id == user_id,
            UserOpeningResultStateModel.result_external_key
            == BidOpeningRoundModel.external_key,
            UserOpeningResultStateModel.state.in_(USER_TERMINAL_STATES),
        )
    )
    export_destination = aliased(SheetDestinationModel)
    shared_destination_alias = aliased(SheetDestinationModel)
    shared_exported = exists(
        select(SheetExportModel.id)
        .join(
            export_destination,
            export_destination.id == SheetExportModel.destination_id,
        )
        .where(
            SheetExportModel.organization_id == organization_id,
            SheetExportModel.result_external_key == BidOpeningRoundModel.external_key,
            SheetExportModel.status == "SUCCEEDED",
            or_(
                export_destination.owner_user_id.is_(None),
                exists(
                    select(shared_destination_alias.id).where(
                        shared_destination_alias.organization_id == organization_id,
                        shared_destination_alias.spreadsheet_id
                        == export_destination.spreadsheet_id,
                        shared_destination_alias.tab_name == export_destination.tab_name,
                        shared_destination_alias.owner_user_id.is_(None),
                    )
                ),
            ),
        )
    )
    return (
        UserOpeningResultMatchModel.organization_id == organization_id,
        UserOpeningResultMatchModel.user_id == user_id,
        UserOpeningResultMatchModel.is_current_match.is_(True),
        ~user_handled,
        ~shared_exported,
    )


def get_visible_result_match(
    db: Session,
    *,
    organization_id: int,
    user_id: int,
    result_id: int,
) -> tuple[BidOpeningRoundModel, UserOpeningResultMatchModel] | None:
    return db.execute(
        select(BidOpeningRoundModel, UserOpeningResultMatchModel)
        .join(
            UserOpeningResultMatchModel,
            UserOpeningResultMatchModel.round_id == BidOpeningRoundModel.id,
        )
        .where(
            BidOpeningRoundModel.id == result_id,
            *visible_result_predicates(organization_id, user_id),
        )
    ).one_or_none()


def load_visible_results(
    db: Session,
    *,
    organization_id: int,
    user_id: int,
    result_ids: list[int],
) -> list[BidOpeningRoundModel]:
    unique_ids = list(dict.fromkeys(result_ids))
    pairs = db.execute(
        select(BidOpeningRoundModel, UserOpeningResultMatchModel)
        .join(
            UserOpeningResultMatchModel,
            UserOpeningResultMatchModel.round_id == BidOpeningRoundModel.id,
        )
        .where(
            BidOpeningRoundModel.id.in_(unique_ids),
            *visible_result_predicates(organization_id, user_id),
        )
    ).all()
    by_id = {round_row.id: (round_row, match) for round_row, match in pairs}
    missing = [result_id for result_id in unique_ids if result_id not in by_id]
    if missing:
        raise ResultAccessError(",".join(str(result_id) for result_id in missing))
    rows: list[BidOpeningRoundModel] = []
    for result_id in unique_ids:
        round_row, match = by_id[result_id]
        round_row.matched_keywords = _split_keywords(match.matched_keywords)
        rows.append(round_row)
    return rows


def list_archived_results(
    db: Session,
    *,
    organization_id: int,
    user_id: int,
    page: int = 1,
    page_size: int = 30,
    result_id: int | None = None,
    now: datetime | None = None,
) -> tuple[list[ArchivedOpeningResult], int]:
    current = _as_utc(now) if now is not None else _utcnow()
    cutoff = current - timedelta(days=ARCHIVE_RETENTION_DAYS)
    archived_by_result_id: dict[int, ArchivedOpeningResult] = {}

    state_statement = (
        select(BidOpeningRoundModel, UserOpeningResultStateModel)
        .join(
            UserOpeningResultStateModel,
            UserOpeningResultStateModel.result_external_key
            == BidOpeningRoundModel.external_key,
        )
        .where(
            UserOpeningResultStateModel.organization_id == organization_id,
            UserOpeningResultStateModel.user_id == user_id,
            UserOpeningResultStateModel.state.in_(USER_TERMINAL_STATES),
            UserOpeningResultStateModel.acted_at >= cutoff,
        )
    )
    if result_id is not None:
        state_statement = state_statement.where(BidOpeningRoundModel.id == result_id)
    for round_row, state in db.execute(state_statement).all():
        handled_at = _as_utc(state.acted_at)
        archived_by_result_id[round_row.id] = ArchivedOpeningResult(
            row=round_row,
            handled_state=state.state,
            handled_at=handled_at,
            can_restore=state.state == "DISMISSED",
        )

    export_destination = aliased(SheetDestinationModel)
    shared_destination_alias = aliased(SheetDestinationModel)
    export_statement = (
        select(BidOpeningRoundModel, SheetExportModel)
        .join(
            UserOpeningResultMatchModel,
            UserOpeningResultMatchModel.round_id == BidOpeningRoundModel.id,
        )
        .join(
            SheetExportModel,
            SheetExportModel.result_external_key == BidOpeningRoundModel.external_key,
        )
        .join(
            export_destination,
            export_destination.id == SheetExportModel.destination_id,
        )
        .where(
            UserOpeningResultMatchModel.organization_id == organization_id,
            UserOpeningResultMatchModel.user_id == user_id,
            SheetExportModel.organization_id == organization_id,
            SheetExportModel.status == "SUCCEEDED",
            SheetExportModel.succeeded_at.is_not(None),
            SheetExportModel.succeeded_at >= cutoff,
            or_(
                export_destination.owner_user_id.is_(None),
                exists(
                    select(shared_destination_alias.id).where(
                        shared_destination_alias.organization_id == organization_id,
                        shared_destination_alias.spreadsheet_id
                        == export_destination.spreadsheet_id,
                        shared_destination_alias.tab_name == export_destination.tab_name,
                        shared_destination_alias.owner_user_id.is_(None),
                    )
                ),
            ),
        )
    )
    if result_id is not None:
        export_statement = export_statement.where(BidOpeningRoundModel.id == result_id)
    for round_row, sheet_export in db.execute(export_statement).all():
        handled_at = _as_utc(sheet_export.succeeded_at)
        existing = archived_by_result_id.get(round_row.id)
        if existing is None or handled_at > existing.handled_at:
            archived_by_result_id[round_row.id] = ArchivedOpeningResult(
                row=round_row,
                handled_state="EXPORTED",
                handled_at=handled_at,
                can_restore=False,
            )

    archived = sorted(
        archived_by_result_id.values(),
        key=lambda item: (item.handled_at, item.row.id),
        reverse=True,
    )
    round_ids = [item.row.id for item in archived]
    if round_ids:
        matches = db.execute(
            select(UserOpeningResultMatchModel).where(
                UserOpeningResultMatchModel.organization_id == organization_id,
                UserOpeningResultMatchModel.user_id == user_id,
                UserOpeningResultMatchModel.round_id.in_(round_ids),
            )
        ).scalars()
        keywords_by_round_id = {
            match.round_id: _split_keywords(match.matched_keywords) for match in matches
        }
        for item in archived:
            item.row.matched_keywords = keywords_by_round_id.get(item.row.id, [])

    total = len(archived)
    start = (page - 1) * page_size
    return archived[start : start + page_size], total


def dismiss_result(
    db: Session,
    *,
    organization_id: int,
    user_id: int,
    result_id: int,
) -> None:
    visible = get_visible_result_match(
        db,
        organization_id=organization_id,
        user_id=user_id,
        result_id=result_id,
    )
    if visible is None:
        raise ResultAccessError(str(result_id))
    _, match = visible
    _set_user_result_state(
        db,
        organization_id=organization_id,
        user_id=user_id,
        result_external_key=match.result_external_key,
        state="DISMISSED",
    )
    db.commit()


def restore_dismissed_result(
    db: Session,
    *,
    organization_id: int,
    user_id: int,
    result_id: int,
    now: datetime | None = None,
) -> bool:
    result_external_key = db.execute(
        select(UserOpeningResultMatchModel.result_external_key)
        .join(
            BidOpeningRoundModel,
            BidOpeningRoundModel.id == UserOpeningResultMatchModel.round_id,
        )
        .where(
            UserOpeningResultMatchModel.organization_id == organization_id,
            UserOpeningResultMatchModel.user_id == user_id,
            BidOpeningRoundModel.id == result_id,
        )
    ).scalar_one_or_none()
    if result_external_key is None:
        raise ResultAccessError(str(result_id))
    state = db.execute(
        select(UserOpeningResultStateModel).where(
            UserOpeningResultStateModel.organization_id == organization_id,
            UserOpeningResultStateModel.user_id == user_id,
            UserOpeningResultStateModel.result_external_key == result_external_key,
            UserOpeningResultStateModel.state == "DISMISSED",
        )
    ).scalar_one_or_none()
    if state is None:
        raise ResultAccessError(str(result_id))
    current = _as_utc(now) if now is not None else _utcnow()
    if _as_utc(state.acted_at) < current - timedelta(days=ARCHIVE_RETENTION_DAYS):
        raise ResultAccessError(str(result_id))
    db.delete(state)
    db.commit()
    return (
        get_visible_result_match(
            db,
            organization_id=organization_id,
            user_id=user_id,
            result_id=result_id,
        )
        is not None
    )


def _set_user_result_state(
    db: Session,
    *,
    organization_id: int,
    user_id: int,
    result_external_key: str,
    state: str,
) -> None:
    row = db.execute(
        select(UserOpeningResultStateModel).where(
            UserOpeningResultStateModel.user_id == user_id,
            UserOpeningResultStateModel.result_external_key == result_external_key,
        )
    ).scalar_one_or_none()
    if row is None:
        row = UserOpeningResultStateModel(
            organization_id=organization_id,
            user_id=user_id,
            result_external_key=result_external_key,
            state=state,
        )
        db.add(row)
    else:
        row.organization_id = organization_id
        row.state = state
        row.acted_at = _utcnow()


def list_sheet_destinations(
    db: Session,
    *,
    organization_id: int,
    user_id: int,
    include_organization: bool = True,
) -> list[SheetDestinationModel]:
    ownership_filter = SheetDestinationModel.owner_user_id == user_id
    if include_organization:
        ownership_filter = or_(
            SheetDestinationModel.owner_user_id.is_(None),
            ownership_filter,
        )
    rows = (
        db.execute(
            select(SheetDestinationModel).where(
                SheetDestinationModel.organization_id == organization_id,
                SheetDestinationModel.is_active.is_(True),
                ownership_filter,
            )
        )
        .scalars()
        .all()
    )
    return sorted(
        rows,
        key=lambda row: (
            not row.is_default,
            row.owner_user_id is None,
            row.label.casefold(),
            row.id,
        ),
    )


def ensure_sheet_target_access(
    db: Session,
    *,
    organization_id: int,
    user_id: int,
    spreadsheet_id: str,
    tab_name: str,
    include_organization: bool = True,
) -> None:
    destination = db.execute(
        select(SheetDestinationModel).where(
            SheetDestinationModel.spreadsheet_id == spreadsheet_id,
            SheetDestinationModel.tab_name == tab_name,
        )
    ).scalar_one_or_none()
    if destination is None:
        return
    allowed = destination.organization_id == organization_id and (
        destination.owner_user_id == user_id
        or (destination.owner_user_id is None and include_organization)
    )
    if not allowed:
        raise SheetDestinationAccessError(
            "다른 사용자 또는 조직이 등록한 Google Sheet 목적지는 확인할 수 없습니다."
        )


def resolve_sheet_destination(
    db: Session,
    *,
    organization_id: int,
    user_id: int,
    destination_id: int | None,
    include_organization: bool = True,
) -> SheetDestinationModel:
    destinations = list_sheet_destinations(
        db,
        organization_id=organization_id,
        user_id=user_id,
        include_organization=include_organization,
    )
    if destination_id is None:
        if not destinations:
            raise SheetDestinationAccessError("사용 가능한 Google Sheet 목적지가 없습니다.")
        destination = destinations[0]
    else:
        destination = next(
            (item for item in destinations if item.id == destination_id),
            None,
        )
        if destination is None:
            raise SheetDestinationAccessError("사용할 수 없는 Google Sheet 목적지입니다.")
    conflicting_organization = db.scalar(
        select(SheetDestinationModel.id).where(
            SheetDestinationModel.id != destination.id,
            SheetDestinationModel.organization_id != organization_id,
            SheetDestinationModel.spreadsheet_id == destination.spreadsheet_id,
            SheetDestinationModel.tab_name == destination.tab_name,
        )
    )
    if conflicting_organization is not None:
        raise SheetDestinationAccessError(
            "이 Google Sheet와 탭은 여러 조직에 중복 등록되어 있어 사용할 수 없습니다."
        )
    return destination


def save_sheet_destination(
    db: Session,
    *,
    organization_id: int,
    user_id: int,
    destination_id: int | None,
    label: str,
    spreadsheet_id: str,
    tab_name: str,
    scope: str,
    is_default: bool,
    can_manage_organization: bool,
) -> SheetDestinationModel:
    owner_user_id = None if scope == "ORGANIZATION" else user_id
    normalized_spreadsheet_id = normalize_spreadsheet_id(spreadsheet_id)
    normalized_tab_name = tab_name.strip() or "개찰결과"
    if owner_user_id is None and not can_manage_organization:
        raise PermissionError("조직 관리자만 조직 공용 Sheet를 설정할 수 있습니다.")

    row = db.get(SheetDestinationModel, destination_id) if destination_id else None
    if row is not None:
        allowed = row.organization_id == organization_id and (
            row.owner_user_id == user_id
            or (row.owner_user_id is None and can_manage_organization)
        )
        if not allowed:
            raise SheetDestinationAccessError("수정할 수 없는 Google Sheet 목적지입니다.")
        if (
            row.owner_user_id != owner_user_id
            or row.spreadsheet_id != normalized_spreadsheet_id
            or row.tab_name != normalized_tab_name
        ):
            raise SheetDestinationConflictError(
                "사용 이력이 연결된 Sheet ID, 탭, 개인/조직 범위는 변경할 수 없습니다. 새 목적지를 추가하세요."
            )
    duplicate_target = db.execute(
        select(SheetDestinationModel).where(
            SheetDestinationModel.spreadsheet_id == normalized_spreadsheet_id,
            SheetDestinationModel.tab_name == normalized_tab_name,
            SheetDestinationModel.id != (destination_id or 0),
        )
    ).scalar_one_or_none()
    if duplicate_target is not None:
        can_reactivate = (
            row is None
            and not duplicate_target.is_active
            and duplicate_target.organization_id == organization_id
            and duplicate_target.owner_user_id == owner_user_id
        )
        if can_reactivate:
            row = duplicate_target
        else:
            raise SheetDestinationConflictError(
                "같은 Google Sheet와 탭이 이미 등록되어 있습니다. 기존 목적지를 사용하세요."
            )
    if row is None:
        row = SheetDestinationModel(
            organization_id=organization_id,
            owner_user_id=owner_user_id,
            label=label,
            spreadsheet_id=normalized_spreadsheet_id,
        )
        db.add(row)

    if is_default:
        default_scope_filter = (
            SheetDestinationModel.owner_user_id.is_(None)
            if owner_user_id is None
            else SheetDestinationModel.owner_user_id == owner_user_id
        )
        db.execute(
            update(SheetDestinationModel)
            .where(
                SheetDestinationModel.organization_id == organization_id,
                default_scope_filter,
            )
            .values(is_default=False)
        )
    row.owner_user_id = owner_user_id
    row.label = label.strip()
    row.spreadsheet_id = normalized_spreadsheet_id
    row.tab_name = normalized_tab_name
    row.is_default = is_default
    row.is_active = True
    try:
        db.commit()
    except IntegrityError as error:
        db.rollback()
        raise SheetDestinationConflictError(
            "같은 Google Sheet와 탭이 이미 이 조직에 등록되어 있습니다."
        ) from error
    db.refresh(row)
    return row


def deactivate_sheet_destination(
    db: Session,
    *,
    organization_id: int,
    user_id: int,
    destination_id: int,
    can_manage_organization: bool,
) -> None:
    row = db.get(SheetDestinationModel, destination_id)
    allowed = row is not None and row.organization_id == organization_id and (
        row.owner_user_id == user_id
        or (row.owner_user_id is None and can_manage_organization)
    )
    if not allowed:
        raise SheetDestinationAccessError("삭제할 수 없는 Google Sheet 목적지입니다.")
    row.is_active = False
    row.is_default = False
    db.commit()


def claim_sheet_exports(
    db: Session,
    *,
    destination: SheetDestinationModel,
    organization_id: int,
    user_id: int,
    rounds: list[BidOpeningRoundModel],
) -> SheetExportClaimBatch:
    now = _utcnow()
    stale_before = now - timedelta(minutes=EXPORT_CLAIM_MINUTES)
    lock_token = str(uuid4())
    physical_destination_filter = (
        SheetDestinationModel.spreadsheet_id == destination.spreadsheet_id,
        SheetDestinationModel.tab_name == destination.tab_name,
        SheetDestinationModel.is_active.is_(True),
    )
    physical_destination_count = db.scalar(
        select(func.count(SheetDestinationModel.id)).where(
            *physical_destination_filter
        )
    )
    if physical_destination_count != 1:
        db.rollback()
        raise SheetExportConflictError(
            "Google Sheet 목적지가 비활성화되었거나 중복 등록되어 있습니다."
        )
    lock_result = db.execute(
        update(SheetDestinationModel)
        .where(
            *physical_destination_filter,
            or_(
                SheetDestinationModel.export_lock_token.is_(None),
                SheetDestinationModel.export_lock_claimed_at.is_(None),
                SheetDestinationModel.export_lock_claimed_at <= stale_before,
            ),
        )
        .values(
            export_lock_token=lock_token,
            export_lock_claimed_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    if lock_result.rowcount != 1:
        db.rollback()
        raise SheetExportConflictError("같은 Sheet의 다른 반영 작업이 이미 진행 중입니다.")

    keys = [round_row.external_key for round_row in rounds]
    existing_rows = (
        db.execute(
            select(SheetExportModel).where(
                SheetExportModel.destination_id == destination.id,
                SheetExportModel.result_external_key.in_(keys),
            )
        )
        .scalars()
        .all()
    )
    existing_by_key = {row.result_external_key: row for row in existing_rows}
    for claim in existing_rows:
        if claim.status == "SUCCEEDED":
            db.rollback()
            raise SheetExportConflictError("이미 이 Sheet에 반영된 결과가 포함되어 있습니다.")
        if claim.status == "PENDING":
            claimed_at = claim.claimed_at
            if claimed_at.tzinfo is None:
                claimed_at = claimed_at.replace(tzinfo=timezone.utc)
            if claimed_at > stale_before:
                db.rollback()
                raise SheetExportConflictError("같은 Sheet 반영 작업이 이미 진행 중입니다.")

    claims: list[SheetExportModel] = []
    for result_external_key in keys:
        claim = existing_by_key.get(result_external_key)
        if claim is None:
            claim = SheetExportModel(
                destination_id=destination.id,
                organization_id=organization_id,
                result_external_key=result_external_key,
                exported_by_user_id=user_id,
                status="PENDING",
                claimed_at=now,
            )
            db.add(claim)
        else:
            claim.organization_id = organization_id
            claim.exported_by_user_id = user_id
            claim.status = "PENDING"
            claim.attempt_count += 1
            claim.error_message = None
            claim.claimed_at = now
            claim.succeeded_at = None
        claims.append(claim)
    try:
        db.commit()
    except IntegrityError as error:
        db.rollback()
        raise SheetExportConflictError("같은 Sheet 반영 요청이 동시에 처리되었습니다.") from error
    return SheetExportClaimBatch(
        destination_id=destination.id,
        lock_token=lock_token,
        records=claims,
    )


def complete_sheet_exports(
    db: Session,
    *,
    claim_batch: SheetExportClaimBatch,
    organization_id: int,
    user_id: int,
) -> None:
    now = _utcnow()
    release_result = db.execute(
        update(SheetDestinationModel)
        .where(SheetDestinationModel.export_lock_token == claim_batch.lock_token)
        .values(export_lock_token=None, export_lock_claimed_at=None)
        .execution_options(synchronize_session=False)
    )
    if release_result.rowcount < 1:
        db.rollback()
        raise SheetExportConflictError(
            "Sheet 반영 잠금이 만료되었습니다. 목록을 새로 확인한 뒤 다시 시도하세요."
        )
    for claimed in claim_batch.records:
        claim = db.get(SheetExportModel, claimed.id)
        claim.status = "SUCCEEDED"
        claim.succeeded_at = now
        claim.error_message = None
        _set_user_result_state(
            db,
            organization_id=organization_id,
            user_id=user_id,
            result_external_key=claim.result_external_key,
            state="EXPORTED",
        )
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise


def fail_sheet_exports(
    db: Session,
    *,
    claim_batch: SheetExportClaimBatch,
    error_message: str,
) -> None:
    db.rollback()
    owns_lock = db.scalar(
        select(
            exists().where(
                SheetDestinationModel.export_lock_token == claim_batch.lock_token
            )
        )
    )
    if not owns_lock:
        db.rollback()
        return
    for claimed in claim_batch.records:
        claim = db.get(SheetExportModel, claimed.id)
        if claim is None or claim.status == "SUCCEEDED":
            continue
        claim.status = "FAILED"
        claim.error_message = error_message[:2000]
    db.execute(
        update(SheetDestinationModel)
        .where(SheetDestinationModel.export_lock_token == claim_batch.lock_token)
        .values(export_lock_token=None, export_lock_claimed_at=None)
        .execution_options(synchronize_session=False)
    )
    db.commit()
