from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.data.database import get_db
from app.data.models import ScraperNoticeModel
from app.g2b.bid_notices.collector import BidNoticeCollectionError, collect_bid_notices
from app.g2b.bid_notices.matching import (
    get_user_bid_notice_profile,
    sync_user_bid_notice_matches,
    update_user_bid_notice_profile,
)
from app.g2b.bid_notices.models import UserBidNoticeMatchModel
from app.g2b.bid_notices.schemas import (
    BidNoticeListItem,
    BidNoticeListResponse,
    BidNoticeProfileResponse,
    BidNoticeProfileUpdateRequest,
    BidNoticeSettingsResponse,
    CollectBidNoticesRequest,
    CollectBidNoticesResponse,
)
from app.g2b.keyword_policy import normalize_keywords
from app.services.auth_service import require_organization_auth


router = APIRouter(prefix="/api/v1/bid-notices", tags=["g2b-bid-notices"])


@router.post("/collect", response_model=CollectBidNoticesResponse)
def collect_bid_notice_data(
    request: CollectBidNoticesRequest,
    auth: dict = Depends(require_organization_auth),
    db: Session = Depends(get_db),
) -> CollectBidNoticesResponse:
    if auth.get("role") != "admin":
        raise HTTPException(status_code=403, detail="시스템 관리자만 공통 원본 수집을 실행할 수 있습니다.")
    try:
        result = collect_bid_notices(
            db,
            start_date=request.start_date,
            end_date=request.end_date,
            business_types=request.business_types,
        )
    except BidNoticeCollectionError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    return CollectBidNoticesResponse(**result)


@router.get("/settings", response_model=BidNoticeSettingsResponse)
def fetch_bid_notice_settings(
    auth: dict = Depends(require_organization_auth),
    db: Session = Depends(get_db),
) -> BidNoticeSettingsResponse:
    profile = get_user_bid_notice_profile(
        db, organization_id=auth["organization_id"], user_id=auth["user_id"]
    )
    db.commit()
    return BidNoticeSettingsResponse(
        profile=BidNoticeProfileResponse(
            enabled=profile.enabled,
            keywords=normalize_keywords(profile.keywords),
            excluded_keywords=normalize_keywords(profile.excluded_keywords),
        )
    )


@router.put("/settings/profile", response_model=BidNoticeProfileResponse)
def save_bid_notice_profile(
    request: BidNoticeProfileUpdateRequest,
    auth: dict = Depends(require_organization_auth),
    db: Session = Depends(get_db),
) -> BidNoticeProfileResponse:
    profile = update_user_bid_notice_profile(
        db,
        organization_id=auth["organization_id"],
        user_id=auth["user_id"],
        enabled=request.enabled,
        keywords=request.keywords,
        excluded_keywords=request.excluded_keywords,
    )
    return BidNoticeProfileResponse(
        enabled=profile.enabled,
        keywords=normalize_keywords(profile.keywords),
        excluded_keywords=normalize_keywords(profile.excluded_keywords),
    )


@router.get("", response_model=BidNoticeListResponse)
def list_bid_notices(
    q: str | None = Query(default=None, max_length=200),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=30, ge=1, le=100),
    auth: dict = Depends(require_organization_auth),
    db: Session = Depends(get_db),
) -> BidNoticeListResponse:
    sync_user_bid_notice_matches(
        db, organization_id=auth["organization_id"], user_id=auth["user_id"]
    )
    db.commit()
    cutoff = datetime.now(timezone.utc) - timedelta(days=14)
    statement = (
        select(ScraperNoticeModel, UserBidNoticeMatchModel.matched_keyword)
        .join(
            UserBidNoticeMatchModel,
            UserBidNoticeMatchModel.notice_id == ScraperNoticeModel.id,
        )
        .where(
            UserBidNoticeMatchModel.user_id == auth["user_id"],
            UserBidNoticeMatchModel.is_current_match.is_(True),
            ScraperNoticeModel.published_at >= cutoff,
        )
    )
    if q and q.strip():
        statement = statement.where(ScraperNoticeModel.title.like(f"%{q.strip()}%"))
    total = db.execute(select(func.count()).select_from(statement.subquery())).scalar_one()
    rows = db.execute(
        statement.order_by(ScraperNoticeModel.published_at.desc(), ScraperNoticeModel.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    items = [
        BidNoticeListItem(
            id=notice.id,
            bid_notice_no=notice.bid_notice_no,
            bid_notice_ord=notice.bid_notice_ord,
            business_name=notice.business_name or notice.title,
            demand_agency_name=notice.demand_agency_name or notice.agency,
            work_type=notice.work_type,
            procurement_type=notice.procurement_type,
            official_base_amount=notice.official_base_amount,
            business_amount=notice.base_amount,
            published_at=notice.published_at,
            deadline_at=notice.deadline_at,
            notice_url=notice.notice_url,
            region_restriction=notice.region_restriction,
            region_restriction_api_status=notice.region_restriction_api_status,
            is_two_stage_bid=notice.is_two_stage_bid,
            matched_keyword=matched_keyword,
        )
        for notice, matched_keyword in rows
    ]
    return BidNoticeListResponse(items=items, total=total, page=page, page_size=page_size)
