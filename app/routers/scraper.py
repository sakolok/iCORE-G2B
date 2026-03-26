from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.data.database import get_db
from app.schemas import ScraperConfig, TriggerScraperRequest, TriggerScraperResponse
from app.services.platform_service import (
    create_scraper_task,
    get_scraper_config,
    upsert_scraper_config,
)

router = APIRouter(prefix="/api/scraper", tags=["scraper"])


@router.get("/config", response_model=ScraperConfig)
def fetch_scraper_config(db: Session = Depends(get_db)) -> ScraperConfig:
    return get_scraper_config(db)


@router.put("/config")
def update_scraper_config(config: ScraperConfig, db: Session = Depends(get_db)) -> dict:
    saved_config = upsert_scraper_config(db, config)
    return {
        "success": True,
        "message": "Scraper 설정이 저장되었습니다.",
        "config": saved_config.model_dump(mode="json"),
    }


@router.post("/trigger", response_model=TriggerScraperResponse)
def trigger_scraper(
    request: TriggerScraperRequest, db: Session = Depends(get_db)
) -> TriggerScraperResponse:
    config = get_scraper_config(db)
    return create_scraper_task(config=config, reason=request.reason)
