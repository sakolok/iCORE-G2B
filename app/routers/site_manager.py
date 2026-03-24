from uuid import uuid4

from fastapi import APIRouter

from app.data.store import store
from app.schemas import BusinessSite, CreateBusinessSiteRequest

router = APIRouter(prefix="/api/sites", tags=["site-manager"])


@router.get("", response_model=list[BusinessSite])
def list_business_sites() -> list[BusinessSite]:
    return store.business_sites


@router.post("", response_model=BusinessSite)
def create_business_site(request: CreateBusinessSiteRequest) -> BusinessSite:
    site = BusinessSite(
        id=str(uuid4()),
        topic=request.topic,
        name=request.name,
        url=request.url,
        status=request.status,
    )
    store.business_sites.append(site)
    return site
