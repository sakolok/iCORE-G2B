from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.data.database import get_db
from app.g2b.opening_results.matching import SheetDestinationAccessError, resolve_sheet_destination
from app.g2b.pre_specifications.client import PreSpecificationApiConfigurationError, PreSpecificationApiError
from app.g2b.pre_specifications.models import PreSpecificationModel, PreSpecificationSheetExportModel
from app.g2b.pre_specifications.schemas import (
    CollectPreSpecificationsRequest,
    PreSpecificationExportRequest,
    PreSpecificationExportResponse,
    PreSpecificationListQuery,
    PreSpecificationListResponse,
    PreSpecificationResponse,
)
from app.g2b.pre_specifications.service import (
    collect_pre_specifications,
    get_pre_specification,
    list_pre_specifications,
    mark_exported,
    response_payload,
    run_scheduled_pre_specifications,
)
from app.g2b.pre_specifications.sheet_export import SHEET_HEADERS, PreSpecificationSheetError, PreSpecificationSheetWriter, build_rows
from app.services.auth_service import require_organization_auth, verify_pre_spec_scheduler_oidc_token, verify_scraper_internal_token


router = APIRouter(prefix="/api/v1/pre-specifications", tags=["g2b-pre-specifications"])


def _can_manage_organization(auth: dict) -> bool:
    return auth.get("role") == "admin" or auth.get("organization_role") == "admin"


@router.post("/collect")
def collect_pre_specification_data(request: CollectPreSpecificationsRequest, auth: dict = Depends(require_organization_auth), db: Session = Depends(get_db)) -> dict:
    if auth.get("role") != "admin":
        raise HTTPException(status_code=403, detail="시스템 관리자만 공통 원본 수집을 실행할 수 있습니다.")
    try:
        return collect_pre_specifications(db, request.start_date, request.end_date)
    except PreSpecificationApiConfigurationError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except PreSpecificationApiError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error


@router.post("/internal/collect")
def collect_pre_specification_data_on_schedule(_: None = Depends(verify_scraper_internal_token), __: None = Depends(verify_pre_spec_scheduler_oidc_token), db: Session = Depends(get_db)) -> dict:
    try:
        return run_scheduled_pre_specifications(db)
    except PreSpecificationApiConfigurationError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except PreSpecificationApiError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error


@router.get("", response_model=PreSpecificationListResponse)
def fetch_pre_specifications(
    q: str | None = None,
    keywords: list[str] = Query(default=[]),
    keyword_mode: str = "OR",
    excluded_keywords: list[str] = Query(default=[]),
    registered_from: date | None = None,
    registered_to: date | None = None,
    demand_agency: str | None = None,
    min_budget: float | None = None,
    max_budget: float | None = None,
    attachment: str = "ALL",
    deadline_status: str = "ALL",
    include_exported: bool = False,
    page: int = 1,
    page_size: int = 30,
    auth: dict = Depends(require_organization_auth),
    db: Session = Depends(get_db),
) -> PreSpecificationListResponse:
    try:
        query = PreSpecificationListQuery(q=q, keywords=keywords, keyword_mode=keyword_mode, excluded_keywords=excluded_keywords, registered_from=registered_from, registered_to=registered_to, demand_agency=demand_agency, min_budget=min_budget, max_budget=max_budget, attachment=attachment, deadline_status=deadline_status, include_exported=include_exported, page=page, page_size=page_size)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    rows, total, exported_ids = list_pre_specifications(db, query, organization_id=auth["organization_id"], user_id=auth["user_id"])
    return PreSpecificationListResponse(items=[PreSpecificationResponse(**response_payload(row, exported=row.bf_spec_rgst_no in exported_ids)) for row in rows], total=total, page=query.page, page_size=query.page_size)


@router.get("/{bf_spec_rgst_no}", response_model=PreSpecificationResponse)
def fetch_pre_specification_detail(bf_spec_rgst_no: str, auth: dict = Depends(require_organization_auth), db: Session = Depends(get_db)) -> PreSpecificationResponse:
    row = get_pre_specification(db, bf_spec_rgst_no)
    if row is None:
        raise HTTPException(status_code=404, detail="사전규격을 찾을 수 없습니다.")
    exported = db.scalar(select(PreSpecificationSheetExportModel.id).where(PreSpecificationSheetExportModel.organization_id == auth["organization_id"], PreSpecificationSheetExportModel.bf_spec_rgst_no == bf_spec_rgst_no, PreSpecificationSheetExportModel.status == "SUCCEEDED")) is not None
    return PreSpecificationResponse(**response_payload(row, exported=exported))


@router.post("/export/sheet", response_model=PreSpecificationExportResponse)
def export_pre_specifications(request: PreSpecificationExportRequest, auth: dict = Depends(require_organization_auth), db: Session = Depends(get_db)) -> PreSpecificationExportResponse:
    try:
        destination = resolve_sheet_destination(db, organization_id=auth["organization_id"], user_id=auth["user_id"], destination_id=request.destination_id, include_organization=_can_manage_organization(auth))
    except SheetDestinationAccessError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    if destination.owner_user_id is None and not _can_manage_organization(auth):
        raise HTTPException(status_code=403, detail="조직 관리자만 조직 공용 Sheet에 반영할 수 있습니다.")
    values = db.scalars(select(PreSpecificationModel).where(PreSpecificationModel.bf_spec_rgst_no.in_(request.bf_spec_rgst_nos))).all()
    by_id = {row.bf_spec_rgst_no: row for row in values}
    selected = [by_id[item_id] for item_id in request.bf_spec_rgst_nos if item_id in by_id]
    missing = [item_id for item_id in request.bf_spec_rgst_nos if item_id not in by_id]
    preview_rows = build_rows(selected)
    inserted_count = 0
    if not request.dry_run and selected:
        try:
            inserted_count = PreSpecificationSheetWriter(destination.spreadsheet_id, destination.tab_name).upsert(preview_rows)
        except PreSpecificationSheetError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        except Exception as error:
            raise HTTPException(status_code=502, detail="Google Sheet 기록에 실패했습니다. 공유 권한을 확인하세요.") from error
        for row in selected:
            existing = db.scalar(select(PreSpecificationSheetExportModel).where(PreSpecificationSheetExportModel.destination_id == destination.id, PreSpecificationSheetExportModel.bf_spec_rgst_no == row.bf_spec_rgst_no))
            if existing is None:
                db.add(PreSpecificationSheetExportModel(destination_id=destination.id, organization_id=auth["organization_id"], bf_spec_rgst_no=row.bf_spec_rgst_no, exported_by_user_id=auth["user_id"], status="SUCCEEDED"))
            else:
                existing.status = "SUCCEEDED"
        db.commit()
        mark_exported(db, organization_id=auth["organization_id"], user_id=auth["user_id"], ids=[row.bf_spec_rgst_no for row in selected])
    return PreSpecificationExportResponse(headers=SHEET_HEADERS, row_count=len(preview_rows), written=not request.dry_run, inserted_count=inserted_count, missing_ids=missing, preview_rows=preview_rows)


@router.post("/sheet/reconcile")
def reconcile_pre_specification_sheet(destination_id: int, auth: dict = Depends(require_organization_auth), db: Session = Depends(get_db)) -> dict:
    try:
        destination = resolve_sheet_destination(db, organization_id=auth["organization_id"], user_id=auth["user_id"], destination_id=destination_id, include_organization=_can_manage_organization(auth))
        ids = PreSpecificationSheetWriter(destination.spreadsheet_id, destination.tab_name).existing_ids()
    except SheetDestinationAccessError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except PreSpecificationSheetError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=502, detail="기존 Google Sheet 확인에 실패했습니다. 공유 권한을 확인하세요.") from error
    known_ids = set(db.scalars(select(PreSpecificationModel.bf_spec_rgst_no).where(PreSpecificationModel.bf_spec_rgst_no.in_(ids))).all())
    mark_exported(db, organization_id=auth["organization_id"], user_id=auth["user_id"], ids=known_ids)
    return {"sheet_record_count": len(ids), "matched_count": len(known_ids)}
