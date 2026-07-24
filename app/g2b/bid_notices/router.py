import json
from datetime import date, datetime, time, timedelta, timezone
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.data.database import get_db
from app.data.models import ScraperNoticeModel
from app.g2b.bid_notices.collector import (
    INDUSTRY_API_ERROR,
    BidNoticeCollectionError,
    collect_bid_notices,
    collect_scheduled_bid_notices,
    fetch_notice_detail_source,
    fetch_industry_restriction_codes,
    matches_icore_industry_code,
)
from app.g2b.bid_notices.matching import (
    dismiss_user_bid_notice,
    get_user_bid_notice_profile,
    restore_user_bid_notice,
    sync_user_bid_notice_matches,
    update_user_bid_notice_profile,
)
from app.g2b.bid_notices.models import (
    UserBidNoticeMatchModel,
    UserBidNoticeStateModel,
)
from app.g2b.bid_notices.schemas import (
    BidNoticeListItem,
    BidNoticeListResponse,
    BidNoticeArchiveResponse,
    BidNoticeAttachment,
    DismissBidNoticeResponse,
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
    RestoreBidNoticeResponse,
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
from app.services.auth_service import (
    require_organization_auth,
    verify_cloud_scheduler_oidc_token,
    verify_scraper_internal_token,
)


router = APIRouter(prefix="/api/v1/bid-notices", tags=["g2b-bid-notices"])
KST = ZoneInfo("Asia/Seoul")


def _destination_response(destination) -> BidNoticeSheetDestinationResponse:
    return BidNoticeSheetDestinationResponse(
        id=destination.id,
        label=destination.label,
        spreadsheet_id=destination.spreadsheet_id,
        tab_name=destination.tab_name,
        scope="PERSONAL",
        is_default=destination.is_default,
    )


def _notice_attachments(notice: ScraperNoticeModel) -> list[BidNoticeAttachment]:
    try:
        source = json.loads(notice.source_payload or "{}")
    except (TypeError, ValueError):
        return []
    if not isinstance(source, dict):
        return []

    attachments: list[BidNoticeAttachment] = []
    seen_urls: set[str] = set()
    for index in range(1, 11):
        url = str(source.get(f"ntceSpecDocUrl{index}") or "").strip()
        parsed = urlparse(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not (parsed.hostname or "").lower().endswith("g2b.go.kr")
            or url in seen_urls
        ):
            continue
        seen_urls.add(url)
        attachments.append(
            BidNoticeAttachment(
                label=str(source.get(f"ntceSpecFileNm{index}") or f"첨부파일 {index}").strip(),
                url=url,
            )
        )
    return attachments


def _hydrate_notice_attachments(notice: ScraperNoticeModel) -> bool:
    if _notice_attachments(notice):
        return False
    try:
        source = json.loads(notice.source_payload or "{}")
    except (TypeError, ValueError):
        source = {}
    if not isinstance(source, dict) or source.get("_attachments_checked"):
        return False
    detail_source = fetch_notice_detail_source(
        notice_no=notice.bid_notice_no or notice.notice_id,
        notice_ord=notice.bid_notice_ord or "00",
        work_type=notice.work_type,
    )
    if detail_source is None:
        return False
    source.update(detail_source)
    source["_attachments_checked"] = True
    notice.source_payload = json.dumps(source, ensure_ascii=False, sort_keys=True, default=str)
    return True


def _notice_response(
    notice: ScraperNoticeModel,
    matched_keyword: str | None,
    *,
    include_attachments: bool = False,
) -> BidNoticeListItem:
    return BidNoticeListItem(
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
        industry_restriction_codes=notice.industry_restriction_codes,
        icore_industry_code_match=notice.icore_industry_code_match,
        is_two_stage_bid=notice.is_two_stage_bid,
        joint_supply_allowed=notice.joint_supply_allowed,
        attachments=_notice_attachments(notice) if include_attachments else [],
        matched_keyword=matched_keyword,
    )


@router.post("/collect", response_model=CollectBidNoticesResponse)
def collect_bid_notice_data(
    request: CollectBidNoticesRequest,
    auth: dict = Depends(require_organization_auth),
    db: Session = Depends(get_db),
) -> CollectBidNoticesResponse:
    if auth.get("role") != "admin":
        raise HTTPException(status_code=403, detail="시스템 관리자만 공통 원본 수집을 실행할 수 있습니다.")
    profile = get_user_bid_notice_profile(
        db, organization_id=auth["organization_id"], user_id=auth["user_id"]
    )
    keywords = normalize_keywords(profile.keywords)
    if not profile.enabled or not keywords:
        raise HTTPException(
            status_code=409,
            detail="수집하려면 조건 설정에서 포함 키워드를 한 개 이상 저장하고 조건 사용을 켜세요.",
        )
    try:
        result = collect_bid_notices(
            db,
            start_date=request.start_date,
            end_date=request.end_date,
            business_types=request.business_types,
            keywords=keywords,
        )
    except BidNoticeCollectionError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    return CollectBidNoticesResponse(**result)


@router.post("/internal/collect", response_model=CollectBidNoticesResponse)
def collect_bid_notice_data_on_schedule(
    _: None = Depends(verify_scraper_internal_token),
    __: None = Depends(verify_cloud_scheduler_oidc_token),
    db: Session = Depends(get_db),
) -> CollectBidNoticesResponse:
    try:
        return CollectBidNoticesResponse(**collect_scheduled_bid_notices(db))
    except BidNoticeCollectionError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error


@router.get("/settings", response_model=BidNoticeSettingsResponse)
def fetch_bid_notice_settings(
    auth: dict = Depends(require_organization_auth),
    db: Session = Depends(get_db),
) -> BidNoticeSettingsResponse:
    profile = get_user_bid_notice_profile(
        db, organization_id=auth["organization_id"], user_id=auth["user_id"]
    )
    destinations = list_sheet_destinations(
        db,
        organization_id=auth["organization_id"],
        user_id=auth["user_id"],
    )
    db.commit()
    return BidNoticeSettingsResponse(
        sheet_service_account_email=get_sheet_service_account_email(),
        profile=BidNoticeProfileResponse(
            enabled=profile.enabled,
            keywords=normalize_keywords(profile.keywords),
            excluded_keywords=normalize_keywords(profile.excluded_keywords),
        ),
        sheet_destinations=[_destination_response(item) for item in destinations],
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
    work_type: str | None = Query(default=None, max_length=40),
    region: str | None = Query(default=None, max_length=80),
    published_from: date | None = None,
    published_to: date | None = None,
    icore_codes_only: bool = False,
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
        .outerjoin(
            UserBidNoticeStateModel,
            and_(
                UserBidNoticeStateModel.user_id == auth["user_id"],
                UserBidNoticeStateModel.notice_id == ScraperNoticeModel.id,
                UserBidNoticeStateModel.state == "DISMISSED",
            ),
        )
        .where(
            UserBidNoticeMatchModel.user_id == auth["user_id"],
            UserBidNoticeMatchModel.is_current_match.is_(True),
            ScraperNoticeModel.published_at >= cutoff,
            UserBidNoticeStateModel.id.is_(None),
        )
    )
    if q and q.strip():
        keyword = q.strip()
        statement = statement.where(
            or_(
                ScraperNoticeModel.title.like(f"%{keyword}%"),
                ScraperNoticeModel.business_name.like(f"%{keyword}%"),
                ScraperNoticeModel.bid_notice_no.like(f"%{keyword}%"),
                ScraperNoticeModel.demand_agency_name.like(f"%{keyword}%"),
                ScraperNoticeModel.agency.like(f"%{keyword}%"),
            )
        )
    work_types = [item.strip() for item in (work_type or "").split(",") if item.strip()]
    if work_types:
        stored_work_types = list(work_types)
        if "일반용역" in work_types:
            stored_work_types.append("용역")
        statement = statement.where(ScraperNoticeModel.work_type.in_(stored_work_types))
    if region and region.strip():
        statement = statement.where(
            ScraperNoticeModel.region_restriction.like(f"%{region.strip()}%")
        )
    if published_from is not None:
        statement = statement.where(
            ScraperNoticeModel.published_at
            >= datetime.combine(published_from, time.min, tzinfo=KST)
        )
    if published_to is not None:
        statement = statement.where(
            ScraperNoticeModel.published_at
            <= datetime.combine(published_to, time.max, tzinfo=KST)
        )
    if icore_codes_only:
        unresolved_rows = db.execute(
            statement.where(ScraperNoticeModel.icore_industry_code_match.is_(None))
        ).all()
        for notice, _ in unresolved_rows:
            (
                notice.industry_restriction_codes,
                notice.industry_restriction_api_status,
            ) = fetch_industry_restriction_codes(
                notice_no=notice.bid_notice_no or notice.notice_id,
                notice_ord=notice.bid_notice_ord or "00",
            )
            notice.icore_industry_code_match = (
                None
                if notice.industry_restriction_api_status == INDUSTRY_API_ERROR
                else matches_icore_industry_code(notice.industry_restriction_codes)
            )
        if unresolved_rows:
            db.commit()
        statement = statement.where(
            or_(
                ScraperNoticeModel.icore_industry_code_match.is_(True),
                ScraperNoticeModel.industry_restriction_api_status == "API_EMPTY",
            )
        )
    total = db.execute(select(func.count()).select_from(statement.subquery())).scalar_one()
    rows = db.execute(
        statement.order_by(ScraperNoticeModel.published_at.desc(), ScraperNoticeModel.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    items = [_notice_response(notice, matched_keyword) for notice, matched_keyword in rows]
    return BidNoticeListResponse(items=items, total=total, page=page, page_size=page_size)


@router.get("/archive", response_model=BidNoticeArchiveResponse)
def list_bid_notice_archive(
    q: str | None = Query(default=None, max_length=200),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=30, ge=1, le=100),
    auth: dict = Depends(require_organization_auth),
    db: Session = Depends(get_db),
) -> BidNoticeArchiveResponse:
    cutoff = datetime.now(timezone.utc) - timedelta(days=14)
    statement = (
        select(ScraperNoticeModel, UserBidNoticeMatchModel.matched_keyword)
        .join(
            UserBidNoticeStateModel,
            and_(
                UserBidNoticeStateModel.notice_id == ScraperNoticeModel.id,
                UserBidNoticeStateModel.user_id == auth["user_id"],
                UserBidNoticeStateModel.state == "DISMISSED",
            ),
        )
        .join(
            UserBidNoticeMatchModel,
            and_(
                UserBidNoticeMatchModel.notice_id == ScraperNoticeModel.id,
                UserBidNoticeMatchModel.user_id == auth["user_id"],
            ),
        )
        .where(ScraperNoticeModel.published_at >= cutoff)
    )
    if q and q.strip():
        statement = statement.where(ScraperNoticeModel.title.like(f"%{q.strip()}%"))
    total = db.execute(select(func.count()).select_from(statement.subquery())).scalar_one()
    rows = db.execute(
        statement.order_by(UserBidNoticeStateModel.acted_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    items = [_notice_response(notice, matched_keyword) for notice, matched_keyword in rows]
    return BidNoticeArchiveResponse(items=items, total=total, page=page, page_size=page_size)


@router.get("/items/{notice_id}", response_model=BidNoticeListItem)
def fetch_bid_notice_detail(
    notice_id: int,
    auth: dict = Depends(require_organization_auth),
    db: Session = Depends(get_db),
) -> BidNoticeListItem:
    row = db.execute(
        select(ScraperNoticeModel, UserBidNoticeMatchModel.matched_keyword)
        .join(
            UserBidNoticeMatchModel,
            UserBidNoticeMatchModel.notice_id == ScraperNoticeModel.id,
        )
        .where(
            ScraperNoticeModel.id == notice_id,
            UserBidNoticeMatchModel.user_id == auth["user_id"],
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="입찰공고를 찾을 수 없습니다.")
    notice, matched_keyword = row
    attachments_updated = _hydrate_notice_attachments(notice)
    if notice.industry_restriction_api_status in {None, INDUSTRY_API_ERROR}:
        notice.industry_restriction_codes, notice.industry_restriction_api_status = (
            fetch_industry_restriction_codes(
                notice_no=notice.bid_notice_no or notice.notice_id,
                notice_ord=notice.bid_notice_ord or "00",
            )
        )
        notice.icore_industry_code_match = (
            None
            if notice.industry_restriction_api_status == INDUSTRY_API_ERROR
            else matches_icore_industry_code(notice.industry_restriction_codes)
        )
        attachments_updated = True
    if attachments_updated:
        db.commit()
    return _notice_response(notice, matched_keyword, include_attachments=True)


@router.delete("/items/{notice_id}", response_model=DismissBidNoticeResponse)
def dismiss_bid_notice(
    notice_id: int,
    auth: dict = Depends(require_organization_auth),
    db: Session = Depends(get_db),
) -> DismissBidNoticeResponse:
    try:
        dismiss_user_bid_notice(
            db,
            organization_id=auth["organization_id"],
            user_id=auth["user_id"],
            notice_id=notice_id,
        )
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return DismissBidNoticeResponse(notice_id=notice_id)


@router.post("/items/{notice_id}/restore", response_model=RestoreBidNoticeResponse)
def restore_bid_notice(
    notice_id: int,
    auth: dict = Depends(require_organization_auth),
    db: Session = Depends(get_db),
) -> RestoreBidNoticeResponse:
    try:
        restore_user_bid_notice(
            db,
            organization_id=auth["organization_id"],
            user_id=auth["user_id"],
            notice_id=notice_id,
        )
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return RestoreBidNoticeResponse(notice_id=notice_id)


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
