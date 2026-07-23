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
    BidNoticeSheetDestinationRequest,
    BidNoticeSheetDestinationResponse,
    BidNoticeSheetDestinationVerifyRequest,
    BidNoticeSheetDestinationVerifyResponse,
    BidNoticeSettingsResponse,
    CollectBidNoticesRequest,
    CollectBidNoticesResponse,
    ExportBidNoticesSheetRequest,
    ExportBidNoticesSheetResponse,
)
from app.g2b.bid_notices.sheet_export import (
    BID_NOTICE_SHEET_HEADERS,
    BidNoticeSheetWriter,
    build_bid_notice_preview_token,
    build_bid_notice_sheet_rows,
    claim_bid_notice_sheet_exports,
    complete_bid_notice_sheet_exports,
    fail_bid_notice_sheet_exports,
)
from app.g2b.keyword_policy import normalize_keywords
from app.g2b.opening_results.matching import (
    SheetDestinationAccessError,
    SheetDestinationConflictError,
    SheetExportConflictError,
    deactivate_sheet_destination,
    ensure_sheet_target_access,
    list_sheet_destinations,
    normalize_spreadsheet_id,
    resolve_sheet_destination,
    save_sheet_destination,
)
from app.g2b.opening_results.sheet_export import (
    SheetExportConfigurationError,
    get_sheet_service_account_email,
)
from app.services.auth_service import require_organization_auth


router = APIRouter(prefix="/api/v1/bid-notices", tags=["g2b-bid-notices"])


def _destination_response(destination) -> BidNoticeSheetDestinationResponse:
    return BidNoticeSheetDestinationResponse(
        id=destination.id,
        label=destination.label,
        spreadsheet_id=destination.spreadsheet_id,
        tab_name=destination.tab_name,
        scope="PERSONAL",
        is_default=destination.is_default,
    )


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


@router.get(
    "/sheet-destinations", response_model=list[BidNoticeSheetDestinationResponse]
)
def fetch_bid_notice_sheet_destinations(
    auth: dict = Depends(require_organization_auth),
    db: Session = Depends(get_db),
) -> list[BidNoticeSheetDestinationResponse]:
    destinations = list_sheet_destinations(
        db,
        organization_id=auth["organization_id"],
        user_id=auth["user_id"],
    )
    return [_destination_response(destination) for destination in destinations]


@router.post(
    "/sheet-destinations/verify",
    response_model=BidNoticeSheetDestinationVerifyResponse,
)
def verify_bid_notice_sheet_destination(
    request: BidNoticeSheetDestinationVerifyRequest,
    auth: dict = Depends(require_organization_auth),
    db: Session = Depends(get_db),
) -> BidNoticeSheetDestinationVerifyResponse:
    try:
        spreadsheet_id = normalize_spreadsheet_id(request.spreadsheet_id)
        ensure_sheet_target_access(
            db,
            organization_id=auth["organization_id"],
            user_id=auth["user_id"],
            spreadsheet_id=spreadsheet_id,
            tab_name=request.tab_name,
        )
        spreadsheet_title, tab_exists, header_status = BidNoticeSheetWriter.from_env(
            spreadsheet_id=spreadsheet_id,
            tab_name=request.tab_name,
        ).verify_connection()
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except SheetDestinationAccessError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except SheetExportConfigurationError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(
            status_code=502,
            detail="Google Sheet 연결 확인에 실패했습니다. 서비스계정 공유 권한을 확인하세요.",
        ) from error
    return BidNoticeSheetDestinationVerifyResponse(
        spreadsheet_id=spreadsheet_id,
        spreadsheet_title=spreadsheet_title,
        tab_name=request.tab_name,
        tab_exists=tab_exists,
        header_status=header_status,
        connection_ready=tab_exists and header_status in {"MATCH", "EMPTY"},
        sheet_service_account_email=get_sheet_service_account_email(),
    )


@router.post(
    "/sheet-destinations", response_model=BidNoticeSheetDestinationResponse
)
def save_bid_notice_sheet_destination(
    request: BidNoticeSheetDestinationRequest,
    auth: dict = Depends(require_organization_auth),
    db: Session = Depends(get_db),
) -> BidNoticeSheetDestinationResponse:
    try:
        destination = save_sheet_destination(
            db,
            organization_id=auth["organization_id"],
            user_id=auth["user_id"],
            destination_id=request.destination_id,
            label=request.label,
            spreadsheet_id=request.spreadsheet_id,
            tab_name=request.tab_name,
            is_default=request.is_default,
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except SheetDestinationAccessError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except SheetDestinationConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return _destination_response(destination)


@router.delete("/sheet-destinations/{destination_id}", status_code=204)
def delete_bid_notice_sheet_destination(
    destination_id: int,
    auth: dict = Depends(require_organization_auth),
    db: Session = Depends(get_db),
) -> None:
    try:
        deactivate_sheet_destination(
            db,
            organization_id=auth["organization_id"],
            user_id=auth["user_id"],
            destination_id=destination_id,
        )
    except SheetDestinationAccessError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.post("/export/sheet", response_model=ExportBidNoticesSheetResponse)
def export_bid_notices_sheet(
    request: ExportBidNoticesSheetRequest,
    auth: dict = Depends(require_organization_auth),
    db: Session = Depends(get_db),
) -> ExportBidNoticesSheetResponse:
    try:
        destination = resolve_sheet_destination(
            db,
            organization_id=auth["organization_id"],
            user_id=auth["user_id"],
            destination_id=request.destination_id,
        )
    except SheetDestinationAccessError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error

    notices = (
        db.execute(
            select(ScraperNoticeModel)
            .join(
                UserBidNoticeMatchModel,
                UserBidNoticeMatchModel.notice_id == ScraperNoticeModel.id,
            )
            .where(
                ScraperNoticeModel.id.in_(request.notice_ids),
                UserBidNoticeMatchModel.user_id == auth["user_id"],
                UserBidNoticeMatchModel.is_current_match.is_(True),
            )
        )
        .scalars()
        .all()
    )
    notice_by_id = {notice.id: notice for notice in notices}
    missing_notice_ids = [
        notice_id for notice_id in request.notice_ids if notice_id not in notice_by_id
    ]
    ordered_notices = [notice_by_id[notice_id] for notice_id in request.notice_ids if notice_id in notice_by_id]
    rows = build_bid_notice_sheet_rows(ordered_notices)
    preview_token = build_bid_notice_preview_token(
        destination=destination,
        notice_ids=request.notice_ids,
        rows=rows,
    )
    if not request.dry_run:
        if missing_notice_ids:
            raise HTTPException(
                status_code=404,
                detail="선택한 입찰공고를 내 검토 목록에서 찾을 수 없습니다.",
            )
        if not rows:
            raise HTTPException(status_code=409, detail="반영할 입찰공고가 없습니다.")
        if request.expected_preview_token is None:
            raise HTTPException(
                status_code=409,
                detail="먼저 미리보기를 확인한 뒤 Google Sheet 반영을 실행하세요.",
            )
        if request.expected_preview_token != preview_token:
            raise HTTPException(
                status_code=409,
                detail="미리보기 이후 입찰공고 또는 Sheet 목적지가 변경되었습니다. 다시 확인하세요.",
            )
        claim = None
        try:
            claim = claim_bid_notice_sheet_exports(
                db,
                destination=destination,
                organization_id=auth["organization_id"],
                user_id=auth["user_id"],
                notices=ordered_notices,
            )
            upsert_result = BidNoticeSheetWriter.from_env(
                spreadsheet_id=destination.spreadsheet_id,
                tab_name=destination.tab_name,
            ).upsert(rows)
            complete_bid_notice_sheet_exports(db, claim=claim)
        except SheetExportConflictError as error:
            if claim:
                fail_bid_notice_sheet_exports(db, claim=claim, error_message=str(error))
            raise HTTPException(status_code=409, detail=str(error)) from error
        except SheetExportConfigurationError as error:
            if claim:
                fail_bid_notice_sheet_exports(db, claim=claim, error_message=str(error))
            raise HTTPException(status_code=503, detail=str(error)) from error
        except Exception as error:
            if claim:
                fail_bid_notice_sheet_exports(db, claim=claim, error_message=str(error))
            raise HTTPException(status_code=502, detail="Google Sheet 기록에 실패했습니다.") from error
        return ExportBidNoticesSheetResponse(
            headers=BID_NOTICE_SHEET_HEADERS,
            requested_notice_count=len(request.notice_ids),
            row_count=len(rows),
            missing_notice_ids=missing_notice_ids,
            written=True,
            inserted_count=upsert_result.inserted_count,
            updated_count=upsert_result.updated_count,
            preview_rows=rows,
            destination_id=destination.id,
            destination_label=destination.label,
            destination_tab_name=destination.tab_name,
            preview_token=preview_token,
        )
    return ExportBidNoticesSheetResponse(
        headers=BID_NOTICE_SHEET_HEADERS,
        requested_notice_count=len(request.notice_ids),
        row_count=len(rows),
        missing_notice_ids=missing_notice_ids,
        written=False,
        inserted_count=0,
        updated_count=0,
        preview_rows=rows,
        destination_id=destination.id,
        destination_label=destination.label,
        destination_tab_name=destination.tab_name,
        preview_token=preview_token,
    )
