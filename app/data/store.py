from datetime import time

from app.schemas import LandingTemplate, ScraperConfig


class InMemoryStore:
    def __init__(self) -> None:
        self.templates: list[LandingTemplate] = [
            LandingTemplate(
                id="clean-campaign",
                name="Clean Campaign",
                description="교육/설명형 랜딩에 맞는 심플한 구성",
                preview_style="left-copy-right-cta",
            ),
            LandingTemplate(
                id="dark-product",
                name="Dark Product",
                description="기술/솔루션 소개에 맞는 다크 톤 구성",
                preview_style="hero-centered-strong-cta",
            ),
            LandingTemplate(
                id="event-highlight",
                name="Event Highlight",
                description="모집/행사 공지에 맞는 카드형 구성",
                preview_style="headline-benefits-action",
            ),
        ]

        self.scraper_config = ScraperConfig(
            enabled=True,
            schedule_mode="daily",
            notify_time=time(hour=9, minute=0),
            interval_minutes=60,
            dedup_mode="notice_id",
            dedup_retention_hours=48,
            receiver_emails=["admin@icore.local"],
            keywords=["클라우드", "AI", "교육"],
        )


store = InMemoryStore()
