import os

from pydantic import BaseModel


class Settings(BaseModel):
    app_name: str = "iCore Integrated Work Platform API"
    environment: str = os.getenv("ENVIRONMENT", "local")
    gcs_bucket: str = os.getenv("GCS_BUCKET", "icore-landing-pages")
    gcp_region: str = os.getenv("GCP_REGION", "asia-northeast3")
    scraper_private_api_base: str = os.getenv(
        "SCRAPER_PRIVATE_API_BASE", "http://scraper.internal"
    )


settings = Settings()
