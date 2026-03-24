from app.core.config import settings
from app.schemas import ScraperConfig, TriggerScraperResponse


def build_scraper_config_payload(config: ScraperConfig) -> dict:
    return {
        "enabled": config.enabled,
        "notify_time": config.notify_time.isoformat(),
        "receiver_email": config.receiver_email,
        "keywords": config.keywords,
        "target_api": settings.scraper_private_api_base,
    }


def build_scraper_trigger_response(reason: str | None) -> TriggerScraperResponse:
    reason_text = reason or "manual request from tool UI"
    return TriggerScraperResponse(
        accepted=True,
        message=f"Scraper 실행 요청이 Private API({settings.scraper_private_api_base})로 전달되었습니다. reason={reason_text}",
    )
