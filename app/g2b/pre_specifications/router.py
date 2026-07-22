from datetime import date
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
    CollectPreSpecificationsRequest,
    CollectPreSpecificationsResponse,
    PreSpecificationListQuery,
    PreSpecificationListResponse,
    PreSpecificationResponse,
)
from app.g2b.pre_specifications.service import (
    collect_pre_specifications,
    get_pre_specification,
    list_pre_specifications,
    response_payload,
)
from app.services.auth_service import require_organization_auth


router = APIRouter(
    prefix="/api/v1/pre-specifications",
    tags=["g2b-pre-specifications"],
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
    _: dict = Depends(require_organization_auth),
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
    rows, total = list_pre_specifications(db, query)
    return PreSpecificationListResponse(
        items=[PreSpecificationResponse(**response_payload(row)) for row in rows],
        total=total,
        page=query.page,
        page_size=query.page_size,
    )


@router.get("/{bf_spec_rgst_no}", response_model=PreSpecificationResponse)
def fetch_pre_specification_detail(
    bf_spec_rgst_no: str,
    _: dict = Depends(require_organization_auth),
    db: Session = Depends(get_db),
) -> PreSpecificationResponse:
    normalized_id = bf_spec_rgst_no.strip()
    if not normalized_id or len(normalized_id) > 80:
        raise HTTPException(
            status_code=422,
            detail="사전규격 등록번호가 올바르지 않습니다.",
        )
    row = get_pre_specification(db, normalized_id)
    if row is None:
        raise HTTPException(status_code=404, detail="사전규격을 찾을 수 없습니다.")
    return PreSpecificationResponse(**response_payload(row))
