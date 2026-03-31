import os

from pydantic import BaseModel


class Settings(BaseModel):
    app_name: str = "iCore Integrated Work Platform API"
    environment: str = os.getenv("ENVIRONMENT", "local")
    database_url: str = os.getenv(
        "DATABASE_URL", "mysql+pymysql://icore:icore@localhost:3306/icore"
    )
    client_web_bucket: str = os.getenv(
        "BUCKET_CLIENT_WEB", os.getenv("GCS_BUCKET", "icore-client-web")
    )
    admin_web_bucket: str = os.getenv("BUCKET_ADMIN_WEB", "icore-admin-web")
    media_assets_bucket: str = os.getenv("BUCKET_MEDIA_ASSETS", "icore-media-assets")
    site_templates_bucket: str = os.getenv("BUCKET_SITE_TEMPLATES", "icore-site-templates")
    landing_cdn_base_url: str = os.getenv("LANDING_CDN_BASE_URL", "")
    gcp_region: str = os.getenv("GCP_REGION", "asia-northeast3")
    scraper_private_api_base: str = os.getenv(
        "SCRAPER_PRIVATE_API_BASE", "http://scraper.internal"
    )
    default_receiver_email: str = os.getenv("DEFAULT_RECEIVER_EMAIL", "admin@icore.local")
    auth_secret_key: str = os.getenv("AUTH_SECRET_KEY", "change-me-in-production")
    default_admin_username: str = os.getenv("DEFAULT_ADMIN_USERNAME", "admin")
    default_admin_password: str = os.getenv("DEFAULT_ADMIN_PASSWORD", "icore1234!")


settings = Settings()
