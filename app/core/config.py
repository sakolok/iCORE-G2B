import os

from pydantic import BaseModel


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


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
    cloud_scheduler_enabled: bool = _env_bool("CLOUD_SCHEDULER_ENABLED", False)
    cloud_scheduler_project_id: str = os.getenv("CLOUD_SCHEDULER_PROJECT_ID", "")
    cloud_scheduler_location: str = os.getenv("CLOUD_SCHEDULER_LOCATION", "asia-northeast3")
    cloud_scheduler_job_id: str = os.getenv("CLOUD_SCHEDULER_JOB_ID", "icore-g2b-scraper-job")
    cloud_scheduler_timezone: str = os.getenv("CLOUD_SCHEDULER_TIMEZONE", "Asia/Seoul")
    cloud_scheduler_target_url: str = os.getenv("CLOUD_SCHEDULER_TARGET_URL", "")
    cloud_scheduler_invoker_service_account: str = os.getenv(
        "CLOUD_SCHEDULER_INVOKER_SERVICE_ACCOUNT", ""
    )
    scraper_internal_token: str = os.getenv("SCRAPER_INTERNAL_TOKEN", "")
    apps_script_webhook_url: str = os.getenv("APPS_SCRIPT_WEBHOOK_URL", "")
    scraper_result_callback_url: str = os.getenv("SCRAPER_RESULT_CALLBACK_URL", "")
    gsheet_id: str = os.getenv("GSHEET_ID", "")
    gsheet_tab_name: str = os.getenv("GSHEET_TAB_NAME", "나라장터 공고 수집 목록")


settings = Settings()
