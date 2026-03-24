from fastapi import APIRouter

from app.data.store import store
from app.schemas import ScraperConfig, TriggerScraperRequest, TriggerScraperResponse
from app.services.scraper_proxy import (
    build_scraper_config_payload,
    build_scraper_trigger_response,
)

router = APIRouter(prefix="/api/scraper", tags=["scraper"])


@router.get("/config", response_model=ScraperConfig)
def get_scraper_config() -> ScraperConfig:
    return store.scraper_config


@router.put("/config")
def update_scraper_config(config: ScraperConfig) -> dict:
    store.scraper_config = config
    payload = build_scraper_config_payload(config)
    return {
        "success": True,
        "message": "Scraper 설정이 저장되었고 Private Subnet API 전달용 payload가 생성되었습니다.",
        "forward_payload": payload,
    }


@router.post("/trigger", response_model=TriggerScraperResponse)
def trigger_scraper(request: TriggerScraperRequest) -> TriggerScraperResponse:
    return build_scraper_trigger_response(request.reason)
