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
    PreSpecificationResponse,
    RestorePreSpecificationResponse,
)
from app.g2b.pre_specifications.service import (
    ARCHIVE_RETENTION_DAYS,
    PreSpecificationAccessError,
    collect_pre_specifications,
    dismiss_pre_specification,
    get_visible_pre_specification,
    list_archived_pre_specifications,
    list_pre_specifications,
    load_visible_pre_specifications,
    response_payload,
    restore_dismissed_pre_specification,
    run_scheduled_pre_specifications,
)
from app.g2b.pre_specifications.sheet_export import (
    PRE_SPECIFICATION_TAB_NAME,
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
    resolve_sheet_destination,
)
from app.services.auth_service import (
    require_organization_auth,
    verify_cloud_scheduler_oidc_token,
    verify_scraper_internal_token,
)


router = APIRouter(
    prefix="/api/v1/pre-specifications",
    tags=["g2b-pre-specifications"],
)


def _can_manage_organization(auth: dict) -> bool:
    return auth.get("organization_role") == "admin" or auth.get("role") == "admin"


def _archived_response(item) -> ArchivedPreSpecificationResponse:
    return ArchivedPreSpecificationResponse(
        **response_payload(item.row),
        handled_state=item.handled_state,
        handled_at=item.handled_at,
        expires_at=item.handled_at + timedelta(days=ARCHIVE_RETENTION_DAYS),
        can_restore=item.can_restore,
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
    can_manage = _can_manage_organization(auth)
    try:
        destination = resolve_sheet_destination(
            db,
            organization_id=auth["organization_id"],
            user_id=auth["user_id"],
            destination_id=request.destination_id,
            include_organization=True,
        )
        if destination.owner_user_id is None and not can_manage:
            raise PermissionError("조직 관리자만 조직 공용 Sheet에 반영할 수 있습니다.")
        selected = load_visible_pre_specifications(
            db,
            organization_id=auth["organization_id"],
            user_id=auth["user_id"],
            bf_spec_rgst_nos=request.bf_spec_rgst_nos,
        )
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
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
                destination.spreadsheet_id
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
        destination_scope=(
            "PERSONAL" if destination.owner_user_id is not None else "ORGANIZATION"
        ),
        destination_tab_name=PRE_SPECIFICATION_TAB_NAME,
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
