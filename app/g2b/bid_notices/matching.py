from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.data.models import ScraperNoticeModel
from app.g2b.bid_notices.models import (
    BidNoticeSheetExportModel,
    UserBidNoticeMatchModel,
    UserBidNoticeProfileModel,
    UserBidNoticeStateModel,
)
from app.g2b.keyword_policy import evaluate_keyword_title, normalize_keywords
from app.g2b.opening_results.models import SheetDestinationModel


MATCH_LOOKBACK_DAYS = 14
ARCHIVE_RETENTION_DAYS = 14


@dataclass(frozen=True)
class ArchivedBidNotice:
    row: ScraperNoticeModel
    matched_keyword: str | None
    handled_state: str
    handled_at: datetime
    can_restore: bool


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def get_user_bid_notice_profile(
    db: Session,
    *,
    organization_id: int,
    user_id: int,
) -> UserBidNoticeProfileModel:
    profile = db.execute(
        select(UserBidNoticeProfileModel).where(UserBidNoticeProfileModel.user_id == user_id)
    ).scalar_one_or_none()
    if profile is None:
        profile = UserBidNoticeProfileModel(
            organization_id=organization_id,
            user_id=user_id,
            enabled=False,
            keywords="",
            excluded_keywords="",
        )
        db.add(profile)
        db.flush()
    return profile


def get_enabled_bid_notice_keywords(db: Session) -> list[str]:
    profiles = db.execute(
        select(UserBidNoticeProfileModel).where(UserBidNoticeProfileModel.enabled.is_(True))
    ).scalars()
    keywords: list[str] = []
    for profile in profiles:
        for keyword in normalize_keywords(profile.keywords):
            if keyword not in keywords:
                keywords.append(keyword)
    return keywords


def sync_user_bid_notice_matches(
    db: Session,
    *,
    organization_id: int,
    user_id: int,
    now: datetime | None = None,
) -> int:
    profile = get_user_bid_notice_profile(
        db, organization_id=organization_id, user_id=user_id
    )
    current = now or _utcnow()
    cutoff = current - timedelta(days=MATCH_LOOKBACK_DAYS)
    notices = db.execute(
        select(ScraperNoticeModel).where(
            ScraperNoticeModel.published_at.is_not(None),
            ScraperNoticeModel.published_at >= cutoff,
        )
    ).scalars().all()
    existing = db.execute(
        select(UserBidNoticeMatchModel).where(UserBidNoticeMatchModel.user_id == user_id)
    ).scalars().all()
    existing_by_notice_id = {item.notice_id: item for item in existing}
    current_ids: set[int] = set()
    changed = 0
    keywords = normalize_keywords(profile.keywords)
    excluded_keywords = normalize_keywords(profile.excluded_keywords)

    if profile.enabled and keywords:
        for notice in notices:
            decision = evaluate_keyword_title(notice.business_name or notice.title, keywords, excluded_keywords)
            if not decision.keep:
                continue
            current_ids.add(notice.id)
            match = existing_by_notice_id.get(notice.id)
            if match is None:
                db.add(
                    UserBidNoticeMatchModel(
                        organization_id=organization_id,
                        user_id=user_id,
                        notice_id=notice.id,
                        matched_keyword=decision.matched_keyword,
                        is_current_match=True,
                    )
                )
                changed += 1
            else:
                if not match.is_current_match or match.matched_keyword != decision.matched_keyword:
                    changed += 1
                match.organization_id = organization_id
                match.matched_keyword = decision.matched_keyword
                match.is_current_match = True
                match.matched_at = current

    for match in existing:
        if match.notice_id not in current_ids and match.is_current_match:
            match.is_current_match = False
            changed += 1
    db.flush()
    return changed


def update_user_bid_notice_profile(
    db: Session,
    *,
    organization_id: int,
    user_id: int,
    enabled: bool,
    keywords: list[str],
    excluded_keywords: list[str],
) -> UserBidNoticeProfileModel:
    profile = get_user_bid_notice_profile(
        db, organization_id=organization_id, user_id=user_id
    )
    profile.organization_id = organization_id
    profile.enabled = enabled
    profile.keywords = ",".join(normalize_keywords(keywords))
    profile.excluded_keywords = ",".join(normalize_keywords(excluded_keywords))
    profile.updated_at = _utcnow()
    sync_user_bid_notice_matches(
        db, organization_id=organization_id, user_id=user_id
    )
    db.commit()
    db.refresh(profile)
    return profile


def _set_user_bid_notice_state(
    db: Session,
    *,
    organization_id: int,
    user_id: int,
    notice_id: int,
    state: str,
) -> None:
    row = db.execute(
        select(UserBidNoticeStateModel).where(
            UserBidNoticeStateModel.user_id == user_id,
            UserBidNoticeStateModel.notice_id == notice_id,
        )
    ).scalar_one_or_none()
    if row is None:
        db.add(
            UserBidNoticeStateModel(
                organization_id=organization_id,
                user_id=user_id,
                notice_id=notice_id,
                state=state,
            )
        )
        return
    row.organization_id = organization_id
    row.state = state
    row.acted_at = _utcnow()


def mark_user_bid_notice_exported(
    db: Session,
    *,
    organization_id: int,
    user_id: int,
    notice_id: int,
) -> None:
    _set_user_bid_notice_state(
        db,
        organization_id=organization_id,
        user_id=user_id,
        notice_id=notice_id,
        state="EXPORTED",
    )


def dismiss_user_bid_notice(
    db: Session,
    *,
    organization_id: int,
    user_id: int,
    notice_id: int,
) -> None:
    visible = db.execute(
        select(UserBidNoticeMatchModel).where(
            UserBidNoticeMatchModel.user_id == user_id,
            UserBidNoticeMatchModel.notice_id == notice_id,
            UserBidNoticeMatchModel.is_current_match.is_(True),
        )
    ).scalar_one_or_none()
    if visible is None:
        raise LookupError("선택한 입찰공고를 내 검토 목록에서 찾을 수 없습니다.")
    _set_user_bid_notice_state(
        db,
        organization_id=organization_id,
        user_id=user_id,
        notice_id=notice_id,
        state="DISMISSED",
    )
    db.commit()


def list_archived_bid_notices(
    db: Session,
    *,
    organization_id: int,
    user_id: int,
    page: int = 1,
    page_size: int = 30,
    q: str | None = None,
    now: datetime | None = None,
) -> tuple[list[ArchivedBidNotice], int]:
    current = _as_utc(now) if now is not None else _utcnow()
    cutoff = current - timedelta(days=ARCHIVE_RETENTION_DAYS)
    archived_by_notice_id: dict[int, ArchivedBidNotice] = {}

    state_statement = (
        select(
            ScraperNoticeModel,
            UserBidNoticeStateModel,
            UserBidNoticeMatchModel.matched_keyword,
        )
        .join(
            UserBidNoticeStateModel,
            UserBidNoticeStateModel.notice_id == ScraperNoticeModel.id,
        )
        .outerjoin(
            UserBidNoticeMatchModel,
            (UserBidNoticeMatchModel.notice_id == ScraperNoticeModel.id)
            & (UserBidNoticeMatchModel.user_id == user_id),
        )
        .where(
            UserBidNoticeStateModel.organization_id == organization_id,
            UserBidNoticeStateModel.user_id == user_id,
            UserBidNoticeStateModel.state == "DISMISSED",
            UserBidNoticeStateModel.acted_at >= cutoff,
        )
    )
    if q and q.strip():
        keyword = q.strip()
        state_statement = state_statement.where(
            (ScraperNoticeModel.title.like(f"%{keyword}%"))
            | (ScraperNoticeModel.business_name.like(f"%{keyword}%"))
        )
    for notice, state, matched_keyword in db.execute(state_statement).all():
        handled_at = _as_utc(state.acted_at)
        archived_by_notice_id[notice.id] = ArchivedBidNotice(
            row=notice,
            matched_keyword=matched_keyword,
            handled_state="DISMISSED",
            handled_at=handled_at,
            can_restore=True,
        )

    export_statement = (
        select(
            ScraperNoticeModel,
            BidNoticeSheetExportModel,
            UserBidNoticeMatchModel.matched_keyword,
        )
        .join(
            BidNoticeSheetExportModel,
            BidNoticeSheetExportModel.notice_id == ScraperNoticeModel.id,
        )
        .join(
            SheetDestinationModel,
            SheetDestinationModel.id == BidNoticeSheetExportModel.destination_id,
        )
        .outerjoin(
            UserBidNoticeMatchModel,
            (UserBidNoticeMatchModel.notice_id == ScraperNoticeModel.id)
            & (UserBidNoticeMatchModel.user_id == user_id),
        )
        .where(
            BidNoticeSheetExportModel.organization_id == organization_id,
            BidNoticeSheetExportModel.user_id == user_id,
            BidNoticeSheetExportModel.status == "SUCCEEDED",
            BidNoticeSheetExportModel.succeeded_at.is_not(None),
            BidNoticeSheetExportModel.succeeded_at >= cutoff,
            SheetDestinationModel.organization_id == organization_id,
            SheetDestinationModel.owner_user_id == user_id,
            SheetDestinationModel.is_active.is_(True),
        )
    )
    if q and q.strip():
        keyword = q.strip()
        export_statement = export_statement.where(
            (ScraperNoticeModel.title.like(f"%{keyword}%"))
            | (ScraperNoticeModel.business_name.like(f"%{keyword}%"))
        )
    for notice, export, matched_keyword in db.execute(export_statement).all():
        handled_at = _as_utc(export.succeeded_at)
        existing = archived_by_notice_id.get(notice.id)
        if existing is None or handled_at > existing.handled_at:
            archived_by_notice_id[notice.id] = ArchivedBidNotice(
                row=notice,
                matched_keyword=matched_keyword,
                handled_state="EXPORTED",
                handled_at=handled_at,
                can_restore=False,
            )

    archived = sorted(
        archived_by_notice_id.values(),
        key=lambda item: (item.handled_at, item.row.id),
        reverse=True,
    )
    total = len(archived)
    start = (page - 1) * page_size
    return archived[start : start + page_size], total


def restore_user_bid_notice(
    db: Session,
    *,
    organization_id: int,
    user_id: int,
    notice_id: int,
) -> bool:
    state = db.execute(
        select(UserBidNoticeStateModel).where(
            UserBidNoticeStateModel.organization_id == organization_id,
            UserBidNoticeStateModel.user_id == user_id,
            UserBidNoticeStateModel.notice_id == notice_id,
            UserBidNoticeStateModel.state == "DISMISSED",
        )
    ).scalar_one_or_none()
    if state is None or _as_utc(state.acted_at) < _utcnow() - timedelta(
        days=ARCHIVE_RETENTION_DAYS
    ):
        raise LookupError("보관함에서 입찰공고를 찾을 수 없습니다.")
    db.delete(state)
    db.commit()
    return True
