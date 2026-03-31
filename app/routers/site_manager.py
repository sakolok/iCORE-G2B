from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.data.database import get_db
from app.schemas import LandingPage, UpdateLandingPageRequest
from app.services.auth_service import require_auth
from app.services.platform_service import (
    delete_landing_page,
    list_landing_pages,
    update_landing_page,
)

router = APIRouter(prefix="/api/sites", tags=["site-manager"])


@router.get("", response_model=list[LandingPage])
def list_business_sites(
    _: dict = Depends(require_auth), db: Session = Depends(get_db)
) -> list[LandingPage]:
    return list_landing_pages(db)


@router.put("/{landing_page_id}", response_model=LandingPage)
def update_business_site(
    landing_page_id: str,
    request: UpdateLandingPageRequest,
    _: dict = Depends(require_auth),
    db: Session = Depends(get_db),
) -> LandingPage:
    try:
        return update_landing_page(db, landing_page_id, request)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.delete("/{landing_page_id}")
def remove_business_site(
    landing_page_id: str,
    _: dict = Depends(require_auth),
    db: Session = Depends(get_db),
) -> dict:
    try:
        delete_landing_page(db, landing_page_id)
        return {"success": True}
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
