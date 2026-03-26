import os

from pydantic import BaseModel


class Settings(BaseModel):
    app_name: str = "iCore Integrated Work Platform API"
    environment: str = os.getenv("ENVIRONMENT", "local")
    database_url: str = os.getenv(
        "DATABASE_URL", "postgresql+psycopg://icore:icore@localhost:5432/icore"
    )
    gcs_bucket: str = os.getenv("GCS_BUCKET", "icore-landing-pages")
    landing_cdn_base_url: str = os.getenv("LANDING_CDN_BASE_URL", "")
    gcp_region: str = os.getenv("GCP_REGION", "asia-northeast3")
    scraper_private_api_base: str = os.getenv(
        "SCRAPER_PRIVATE_API_BASE", "http://scraper.internal"
    )
    default_receiver_email: str = os.getenv("DEFAULT_RECEIVER_EMAIL", "admin@icore.local")


settings = Settings()
