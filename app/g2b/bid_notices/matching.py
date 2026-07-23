from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.data.models import ScraperNoticeModel
from app.g2b.bid_notices.models import UserBidNoticeMatchModel, UserBidNoticeProfileModel
from app.g2b.keyword_policy import evaluate_keyword_title, normalize_keywords


MATCH_LOOKBACK_DAYS = 14


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


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
