import hmac
from datetime import date
from datetime import timedelta
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.data.database import get_db
from app.g2b.pre_specifications.client import (
    PreSpecificationApiConfigurationError,
    PreSpecificationApiError,
)
from app.g2b.keyword_policy import normalize_keywords
from app.g2b.pre_specifications.schemas import (
    ArchivedPreSpecificationListResponse,
    ArchivedPreSpecificationResponse,
    CollectPreSpecificationsRequest,
    CollectPreSpecificationsResponse,
    DismissPreSpecificationResponse,
    ExportPreSpecificationsSheetRequest,
    ExportPreSpecificationsSheetResponse,
    PreSpecificationListQuery,
    PreSpecificationListResponse,
    PreSpecificationProfileResponse,
    PreSpecificationProfileUpdateRequest,
    PreSpecificationResponse,
    PreSpecificationSettingsResponse,
    PreSpecificationSheetDestinationUpsertRequest,
    PreSpecificationSheetDestinationVerifyRequest,
    RestorePreSpecificationResponse,
)
from app.g2b.pre_specifications.service import (
    ARCHIVE_RETENTION_DAYS,
    PreSpecificationAccessError,
    collect_pre_specifications,
    dismiss_pre_specification,
    get_user_pre_specification_profile,
    get_visible_pre_specification,
    list_archived_pre_specifications,
    list_pre_specifications,
    load_visible_pre_specifications,
    response_payload,
    restore_dismissed_pre_specification,
    run_scheduled_pre_specifications,
    update_user_pre_specification_profile,
)
from app.g2b.pre_specifications.sheet_export import (
    SHEET_HEADERS,
    PreSpecificationSheetError,
    PreSpecificationSheetExportConflictError,
    PreSpecificationSheetWriter,
    build_sheet_preview_token,
    build_sheet_rows,
    claim_sheet_exports,
    complete_sheet_exports,
    fail_sheet_exports,
)
from app.g2b.opening_results.matching import (
    SheetDestinationAccessError,
    SheetDestinationConflictError,
    deactivate_sheet_destination,
    ensure_sheet_target_access,
    list_sheet_destinations,
    normalize_spreadsheet_id,
    resolve_sheet_destination,
    save_sheet_destination,
)
from app.g2b.opening_results.schemas import (
    SheetDestinationResponse,
    SheetDestinationVerifyResponse,
)
from app.g2b.opening_results.sheet_export import get_sheet_service_account_email
from app.services.auth_service import (
    require_organization_auth,
    verify_cloud_scheduler_oidc_token,
    verify_scraper_internal_token,
)


router = APIRouter(
    prefix="/api/v1/pre-specifications",
    tags=["g2b-pre-specifications"],
)


def _archived_response(item) -> ArchivedPreSpecificationResponse:
    return ArchivedPreSpecificationResponse(
        **response_payload(item.row),
        handled_state=item.handled_state,
        handled_at=item.handled_at,
        expires_at=item.handled_at + timedelta(days=ARCHIVE_RETENTION_DAYS),
        can_restore=item.can_restore,
    )


def _destination_response(destination) -> SheetDestinationResponse:
    return SheetDestinationResponse(
        id=destination.id,
        label=destination.label,
        spreadsheet_id=destination.spreadsheet_id,
        tab_name=destination.tab_name,
        scope="PERSONAL",
        is_default=destination.is_default,
    )


@router.post("/collect", response_model=CollectPreSpecificationsResponse)
def collect_pre_specification_data(
    request: CollectPreSpecificationsRequest,
    auth: dict = Depends(require_organization_auth),
    db: Session = Depends(get_db),
) -> CollectPreSpecificationsResponse:
    if auth.get("role") != "admin":
        raise HTTPException(
            status_code=403,
            detail="시스템 관리자만 공통 원본 수집을 실행할 수 있습니다.",
        )
    try:
        result = collect_pre_specifications(
            db,
            request.start_date,
            request.end_date,
        )
        return CollectPreSpecificationsResponse(**result)
    except PreSpecificationApiConfigurationError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except PreSpecificationApiError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error


@router.post("/internal/collect", response_model=CollectPreSpecificationsResponse)
def collect_pre_specification_data_on_schedule(
    _: None = Depends(verify_scraper_internal_token),
    __: None = Depends(verify_cloud_scheduler_oidc_token),
    db: Session = Depends(get_db),
) -> CollectPreSpecificationsResponse:
    try:
        return CollectPreSpecificationsResponse(
            **run_scheduled_pre_specifications(db)
        )
    except PreSpecificationApiConfigurationError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except PreSpecificationApiError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error


@router.get("", response_model=PreSpecificationListResponse)
def fetch_pre_specifications(
    q: str | None = Query(default=None, max_length=200),
    keywords: list[str] = Query(default=[]),
    keyword_mode: Literal["AND", "OR"] = "OR",
    excluded_keywords: list[str] = Query(default=[]),
    registered_from: date | None = None,
    registered_to: date | None = None,
    demand_agency: str | None = Query(default=None, max_length=240),
    min_budget: Decimal | None = Query(default=None, ge=0),
    max_budget: Decimal | None = Query(default=None, ge=0),
    attachment: Literal["ALL", "HAS", "NONE"] = "ALL",
    deadline_status: Literal[
        "ALL",
        "OPEN",
        "TODAY",
        "CLOSED",
        "UNKNOWN",
    ] = "ALL",
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=30, ge=1, le=100),
    auth: dict = Depends(require_organization_auth),
    db: Session = Depends(get_db),
) -> PreSpecificationListResponse:
    try:
        query = PreSpecificationListQuery(
            q=q,
            keywords=keywords,
            keyword_mode=keyword_mode,
            excluded_keywords=excluded_keywords,
            registered_from=registered_from,
            registered_to=registered_to,
            demand_agency=demand_agency,
            min_budget=min_budget,
            max_budget=max_budget,
            attachment=attachment,
            deadline_status=deadline_status,
            page=page,
            page_size=page_size,
        )
    except ValidationError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    rows, total = list_pre_specifications(
        db,
        query,
        organization_id=auth["organization_id"],
        user_id=auth["user_id"],
    )
    return PreSpecificationListResponse(
        items=[PreSpecificationResponse(**response_payload(row)) for row in rows],
        total=total,
        page=query.page,
        page_size=query.page_size,
    )


@router.get("/settings", response_model=PreSpecificationSettingsResponse)
def fetch_pre_specification_settings(
    auth: dict = Depends(require_organization_auth),
    db: Session = Depends(get_db),
) -> PreSpecificationSettingsResponse:
    profile = get_user_pre_specification_profile(
        db,
        organization_id=auth["organization_id"],
        user_id=auth["user_id"],
    )
    destinations = list_sheet_destinations(
        db,
        organization_id=auth["organization_id"],
        user_id=auth["user_id"],
    )
    return PreSpecificationSettingsResponse(
        sheet_service_account_email=get_sheet_service_account_email(),
        profile=PreSpecificationProfileResponse(
            enabled=profile.enabled,
            keywords=normalize_keywords(profile.keywords),
            excluded_keywords=normalize_keywords(profile.excluded_keywords),
        ),
        sheet_destinations=[_destination_response(item) for item in destinations],
    )


@router.put("/settings/profile", response_model=PreSpecificationProfileResponse)
def save_pre_specification_profile(
    request: PreSpecificationProfileUpdateRequest,
    auth: dict = Depends(require_organization_auth),
    db: Session = Depends(get_db),
) -> PreSpecificationProfileResponse:
    profile = update_user_pre_specification_profile(
        db,
        organization_id=auth["organization_id"],
        user_id=auth["user_id"],
        enabled=request.enabled,
        keywords=request.keywords,
        excluded_keywords=request.excluded_keywords,
    )
    return PreSpecificationProfileResponse(
        enabled=profile.enabled,
        keywords=normalize_keywords(profile.keywords),
        excluded_keywords=normalize_keywords(profile.excluded_keywords),
    )


@router.get(
    "/sheet-destinations",
    response_model=list[SheetDestinationResponse],
)
def fetch_pre_specification_sheet_destinations(
    auth: dict = Depends(require_organization_auth),
    db: Session = Depends(get_db),
) -> list[SheetDestinationResponse]:
    destinations = list_sheet_destinations(
        db,
        organization_id=auth["organization_id"],
        user_id=auth["user_id"],
    )
    return [_destination_response(item) for item in destinations]


@router.post(
    "/sheet-destinations/verify",
    response_model=SheetDestinationVerifyResponse,
)
def verify_pre_specification_sheet_destination(
    request: PreSpecificationSheetDestinationVerifyRequest,
    auth: dict = Depends(require_organization_auth),
    db: Session = Depends(get_db),
) -> SheetDestinationVerifyResponse:
    try:
        spreadsheet_id = normalize_spreadsheet_id(request.spreadsheet_id)
        ensure_sheet_target_access(
            db,
            organization_id=auth["organization_id"],
            user_id=auth["user_id"],
            spreadsheet_id=spreadsheet_id,
            tab_name=request.tab_name,
        )
        verification = PreSpecificationSheetWriter.from_env(
            spreadsheet_id,
            request.tab_name,
        ).verify_connection()
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except PreSpecificationSheetError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(
            status_code=502,
            detail="Google Sheet 연결 확인에 실패했습니다. 서비스계정 공유 권한을 확인하세요.",
        ) from error
    return SheetDestinationVerifyResponse(
        spreadsheet_id=spreadsheet_id,
        spreadsheet_title=verification.spreadsheet_title,
        tab_name=request.tab_name,
        tab_exists=verification.tab_exists,
        header_status=verification.header_status,
        connection_ready=(
            verification.tab_exists
            and verification.header_status in {"MATCH", "EMPTY"}
        ),
        sheet_service_account_email=get_sheet_service_account_email(),
    )


@router.post(
    "/sheet-destinations",
    response_model=SheetDestinationResponse,
)
def save_pre_specification_sheet_destination(
    request: PreSpecificationSheetDestinationUpsertRequest,
    auth: dict = Depends(require_organization_auth),
    db: Session = Depends(get_db),
) -> SheetDestinationResponse:
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
def delete_pre_specification_sheet_destination(
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


@router.get("/archive", response_model=ArchivedPreSpecificationListResponse)
def fetch_archived_pre_specifications(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=30, ge=1, le=100),
    auth: dict = Depends(require_organization_auth),
    db: Session = Depends(get_db),
) -> ArchivedPreSpecificationListResponse:
    rows, total = list_archived_pre_specifications(
        db,
        organization_id=auth["organization_id"],
        user_id=auth["user_id"],
        page=page,
        page_size=page_size,
    )
    return ArchivedPreSpecificationListResponse(
        items=[_archived_response(item) for item in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get(
    "/archive/{bf_spec_rgst_no}",
    response_model=ArchivedPreSpecificationResponse,
)
def fetch_archived_pre_specification_detail(
    bf_spec_rgst_no: str,
    auth: dict = Depends(require_organization_auth),
    db: Session = Depends(get_db),
) -> ArchivedPreSpecificationResponse:
    rows, _ = list_archived_pre_specifications(
        db,
        organization_id=auth["organization_id"],
        user_id=auth["user_id"],
        page=1,
        page_size=1,
        bf_spec_rgst_no=bf_spec_rgst_no.strip(),
    )
    if not rows:
        raise HTTPException(
            status_code=404,
            detail="14일 보관함에서 사전규격을 찾을 수 없습니다.",
        )
    return _archived_response(rows[0])


@router.post(
    "/export/sheet",
    response_model=ExportPreSpecificationsSheetResponse,
)
def export_pre_specifications_sheet(
    request: ExportPreSpecificationsSheetRequest,
    auth: dict = Depends(require_organization_auth),
    db: Session = Depends(get_db),
) -> ExportPreSpecificationsSheetResponse:
    try:
        destination = resolve_sheet_destination(
            db,
            organization_id=auth["organization_id"],
            user_id=auth["user_id"],
            destination_id=request.destination_id,
        )
        selected = load_visible_pre_specifications(
            db,
            organization_id=auth["organization_id"],
            user_id=auth["user_id"],
            bf_spec_rgst_nos=request.bf_spec_rgst_nos,
        )
    except SheetDestinationAccessError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except PreSpecificationAccessError as error:
        raise HTTPException(
            status_code=404,
            detail="선택한 사전규격을 내 검토 목록에서 찾을 수 없습니다.",
        ) from error

    rows = build_sheet_rows(selected)
    preview_token = build_sheet_preview_token(
        destination_id=destination.id,
        spreadsheet_id=destination.spreadsheet_id,
        tab_name=destination.tab_name,
        bf_spec_rgst_nos=request.bf_spec_rgst_nos,
        rows=rows,
    )
    written = False
    inserted_count = 0
    updated_count = 0
    claim_batch = None
    if not request.dry_run:
        if request.expected_preview_token is None:
            raise HTTPException(
                status_code=409,
                detail="먼저 미리보기를 확인한 뒤 Google Sheet 반영을 실행하세요.",
            )
        if not hmac.compare_digest(request.expected_preview_token, preview_token):
            raise HTTPException(
                status_code=409,
                detail="미리보기 이후 결과 또는 Sheet 목적지가 변경되었습니다.",
            )
        try:
            claim_batch = claim_sheet_exports(
                db,
                destination=destination,
                organization_id=auth["organization_id"],
                user_id=auth["user_id"],
                rows=selected,
            )
            writer = PreSpecificationSheetWriter.from_env(
                destination.spreadsheet_id,
                destination.tab_name,
            )
            upsert_result = writer.upsert(rows)
            inserted_count = upsert_result.inserted_count
            updated_count = upsert_result.updated_count
            complete_sheet_exports(
                db,
                claim_batch=claim_batch,
                organization_id=auth["organization_id"],
                user_id=auth["user_id"],
            )
            written = True
        except PreSpecificationSheetExportConflictError as error:
            if claim_batch is not None:
                fail_sheet_exports(
                    db,
                    claim_batch=claim_batch,
                    error_message=str(error),
                )
            raise HTTPException(status_code=409, detail=str(error)) from error
        except PreSpecificationSheetError as error:
            if claim_batch is not None:
                fail_sheet_exports(
                    db,
                    claim_batch=claim_batch,
                    error_message=str(error),
                )
            raise HTTPException(status_code=503, detail=str(error)) from error
        except Exception as error:
            if claim_batch is not None:
                fail_sheet_exports(
                    db,
                    claim_batch=claim_batch,
                    error_message=str(error),
                )
            raise HTTPException(
                status_code=502,
                detail="Google Sheet 기록에 실패했습니다.",
            ) from error
    return ExportPreSpecificationsSheetResponse(
        headers=SHEET_HEADERS,
        requested_count=len(request.bf_spec_rgst_nos),
        row_count=len(rows),
        written=written,
        inserted_count=inserted_count,
        updated_count=updated_count,
        preview_rows=rows,
        destination_id=destination.id,
        destination_label=destination.label,
        destination_scope="PERSONAL",
        destination_tab_name=destination.tab_name,
        preview_token=preview_token,
    )


@router.post(
    "/{bf_spec_rgst_no}/restore",
    response_model=RestorePreSpecificationResponse,
)
def restore_pre_specification_to_inbox(
    bf_spec_rgst_no: str,
    auth: dict = Depends(require_organization_auth),
    db: Session = Depends(get_db),
) -> RestorePreSpecificationResponse:
    normalized_id = bf_spec_rgst_no.strip()
    try:
        visible = restore_dismissed_pre_specification(
            db,
            organization_id=auth["organization_id"],
            user_id=auth["user_id"],
            bf_spec_rgst_no=normalized_id,
        )
    except PreSpecificationAccessError as error:
        raise HTTPException(
            status_code=404,
            detail="실행취소할 제외 상태를 찾을 수 없습니다.",
        ) from error
    return RestorePreSpecificationResponse(
        bf_spec_rgst_no=normalized_id,
        visible=visible,
    )


@router.delete(
    "/{bf_spec_rgst_no}",
    response_model=DismissPreSpecificationResponse,
)
def delete_pre_specification_from_inbox(
    bf_spec_rgst_no: str,
    auth: dict = Depends(require_organization_auth),
    db: Session = Depends(get_db),
) -> DismissPreSpecificationResponse:
    normalized_id = bf_spec_rgst_no.strip()
    try:
        dismiss_pre_specification(
            db,
            organization_id=auth["organization_id"],
            user_id=auth["user_id"],
            bf_spec_rgst_no=normalized_id,
        )
    except PreSpecificationAccessError as error:
        raise HTTPException(status_code=404, detail="사전규격을 찾을 수 없습니다.") from error
    return DismissPreSpecificationResponse(bf_spec_rgst_no=normalized_id)


@router.get("/{bf_spec_rgst_no}", response_model=PreSpecificationResponse)
def fetch_pre_specification_detail(
    bf_spec_rgst_no: str,
    auth: dict = Depends(require_organization_auth),
    db: Session = Depends(get_db),
) -> PreSpecificationResponse:
    normalized_id = bf_spec_rgst_no.strip()
    if not normalized_id or len(normalized_id) > 80:
        raise HTTPException(
            status_code=422,
            detail="사전규격 등록번호가 올바르지 않습니다.",
        )
    row = get_visible_pre_specification(
        db,
        organization_id=auth["organization_id"],
        user_id=auth["user_id"],
        bf_spec_rgst_no=normalized_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="사전규격을 찾을 수 없습니다.")
    return PreSpecificationResponse(**response_payload(row))
