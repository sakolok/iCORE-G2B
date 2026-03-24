from fastapi import APIRouter

from app.data.store import store
from app.schemas import DeployRequest, DeployResponse, LandingTemplate
from app.services.storage_deployer import create_static_deployment

router = APIRouter(prefix="/api/builder", tags=["builder"])


@router.get("/templates", response_model=list[LandingTemplate])
def list_templates() -> list[LandingTemplate]:
    return store.templates


@router.post("/deploy", response_model=DeployResponse)
def deploy_landing_page(request: DeployRequest) -> DeployResponse:
    return create_static_deployment(request)
