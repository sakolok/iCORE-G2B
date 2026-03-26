from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.data.database import get_db
from app.schemas import DeployRequest, DeployResponse, LandingTemplate
from app.services.platform_service import create_landing_page, list_templates

router = APIRouter(prefix="/api/builder", tags=["builder"])


@router.get("/templates", response_model=list[LandingTemplate])
def list_landing_templates(db: Session = Depends(get_db)) -> list[LandingTemplate]:
    return list_templates(db)


@router.post("/deploy", response_model=DeployResponse)
def deploy_landing_page(
    request: DeployRequest, db: Session = Depends(get_db)
) -> DeployResponse:
    try:
        return create_landing_page(db, request)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
