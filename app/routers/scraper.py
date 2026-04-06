from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.data.database import get_db
from app.schemas import ScraperConfig, TriggerScraperRequest, TriggerScraperResponse
from app.services.auth_service import require_auth
from app.services.platform_service import (
    create_scraper_task,
    get_scraper_config,
    upsert_scraper_config,
)
from app.services.cloud_scheduler_service import sync_scheduler_job

router = APIRouter(prefix="/api/scraper", tags=["scraper"])


@router.get("/config", response_model=ScraperConfig)
def fetch_scraper_config(
    _: dict = Depends(require_auth), db: Session = Depends(get_db)
) -> ScraperConfig:
    return get_scraper_config(db)


@router.put("/config")
def update_scraper_config(
    config: ScraperConfig,
    _: dict = Depends(require_auth),
    db: Session = Depends(get_db),
) -> dict:
    saved_config = upsert_scraper_config(db, config)
    scheduler_status = sync_scheduler_job(saved_config)
    return {
        "success": True,
        "message": "Scraper 설정이 저장되었습니다.",
        "config": saved_config.model_dump(mode="json"),
        "scheduler": scheduler_status.model_dump(mode="json"),
    }


@router.post("/trigger", response_model=TriggerScraperResponse)
def trigger_scraper(
    request: TriggerScraperRequest,
    _: dict = Depends(require_auth),
    db: Session = Depends(get_db),
) -> TriggerScraperResponse:
    config = get_scraper_config(db)
    return create_scraper_task(config=config, reason=request.reason)
