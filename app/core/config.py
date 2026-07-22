import os

from pydantic import BaseModel


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_csv(name: str, default: str = "") -> tuple[str, ...]:
    raw = os.getenv(name, default)
    return tuple(item.strip().lower() for item in raw.split(",") if item.strip())


class Settings(BaseModel):
    app_name: str = "iCore G2B API"
    environment: str = os.getenv("ENVIRONMENT", "local")
    database_url: str = os.getenv(
        "DATABASE_URL", "mysql+pymysql://icore:icore@localhost:3306/icore"
    )
    gcp_region: str = os.getenv("GCP_REGION", "asia-northeast3")
    scraper_private_api_base: str = os.getenv(
        "SCRAPER_PRIVATE_API_BASE", "http://scraper.internal"
    )
    default_receiver_email: str = os.getenv("DEFAULT_RECEIVER_EMAIL", "admin@icore.local")
    auth_secret_key: str = os.getenv("AUTH_SECRET_KEY", "change-me-in-production")
    auth_token_ttl_hours: int = int(os.getenv("AUTH_TOKEN_TTL_HOURS", "8"))
    legacy_password_login_enabled: bool = _env_bool(
        "LEGACY_PASSWORD_LOGIN_ENABLED", False
    )
    single_user_mode_enabled: bool = _env_bool("SINGLE_USER_MODE_ENABLED", False)
    single_user_username: str = os.getenv("SINGLE_USER_USERNAME", "admin")
    cors_allowed_origins: tuple[str, ...] = _env_csv(
        "CORS_ALLOWED_ORIGINS",
        "http://localhost:5173,http://127.0.0.1:5173",
    )
    default_admin_username: str = os.getenv("DEFAULT_ADMIN_USERNAME", "admin")
    default_admin_password: str = os.getenv("DEFAULT_ADMIN_PASSWORD", "icore1234!")
    default_admin_email: str = os.getenv("DEFAULT_ADMIN_EMAIL", "")
    google_oauth_client_id: str = os.getenv("GOOGLE_OAUTH_CLIENT_ID", "")
    allowed_login_domains: tuple[str, ...] = _env_csv(
        "ALLOWED_LOGIN_DOMAINS", "iceu.kr,iceu.co.kr"
    )
    default_organization_name: str = os.getenv("DEFAULT_ORGANIZATION_NAME", "iCore")
    default_organization_slug: str = os.getenv("DEFAULT_ORGANIZATION_SLUG", "icore")
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
    g2b_award_service_key: str = os.getenv(
        "G2B_AWARD_SERVICE_KEY", os.getenv("G2B_SERVICE_KEY", "")
    )
    g2b_award_scheduler_target_url: str = os.getenv(
        "G2B_AWARD_SCHEDULER_TARGET_URL", ""
    )
    g2b_award_scheduler_oidc_audience: str = os.getenv(
        "G2B_AWARD_SCHEDULER_OIDC_AUDIENCE", ""
    )
    apps_script_webhook_url: str = os.getenv("APPS_SCRIPT_WEBHOOK_URL", "")
    scraper_result_callback_url: str = os.getenv("SCRAPER_RESULT_CALLBACK_URL", "")
    gsheet_id: str = os.getenv("GSHEET_ID", "")
    gsheet_opening_result_id: str = os.getenv(
        "GSHEET_OPENING_RESULT_ID", os.getenv("GSHEET_ID", "")
    )
    gsheet_tab_name: str = os.getenv("GSHEET_TAB_NAME", "나라장터 공고 수집 목록")


settings = Settings()


def validate_runtime_settings(value: Settings) -> None:
    environment = value.environment.strip().lower()
    if environment not in {"production", "staging"}:
        return

    errors: list[str] = []
    if environment == "production":
        if value.auth_secret_key.strip() in {
            "change-me-in-production",
            "local-only-change-me-at-least-32-characters",
        } or len(value.auth_secret_key.strip()) < 32:
            errors.append("AUTH_SECRET_KEY는 32자 이상의 운영 전용 비밀값이어야 합니다.")
        if (
            value.default_admin_password == "icore1234!"
            or len(value.default_admin_password.strip()) < 12
        ):
            errors.append("DEFAULT_ADMIN_PASSWORD는 12자 이상의 운영 전용 비밀값이어야 합니다.")
        if not value.google_oauth_client_id.strip():
            errors.append("GOOGLE_OAUTH_CLIENT_ID는 운영 환경에서 필수입니다.")
        if set(value.allowed_login_domains) != {"iceu.kr", "iceu.co.kr"}:
            errors.append("ALLOWED_LOGIN_DOMAINS는 iceu.kr,iceu.co.kr만 허용해야 합니다.")
        if value.legacy_password_login_enabled:
            errors.append("LEGACY_PASSWORD_LOGIN_ENABLED는 운영 환경에서 사용할 수 없습니다.")
        if not value.cors_allowed_origins or "*" in value.cors_allowed_origins:
            errors.append("CORS_ALLOWED_ORIGINS에는 운영 프론트 주소를 명시해야 합니다.")
        if len(value.scraper_internal_token.strip()) < 32:
            errors.append("SCRAPER_INTERNAL_TOKEN은 32자 이상의 비밀값이어야 합니다.")
        if not value.g2b_award_service_key.strip():
            errors.append("G2B_AWARD_SERVICE_KEY 또는 G2B_SERVICE_KEY가 필요합니다.")
        scheduler_target = value.g2b_award_scheduler_target_url.strip().rstrip("/")
        if (
            not scheduler_target.startswith("https://")
            or not scheduler_target.endswith("/api/v1/results/internal/collect")
        ):
            errors.append(
                "G2B_AWARD_SCHEDULER_TARGET_URL은 HTTPS 내부 수집 API 주소여야 합니다."
            )
        scheduler_service_account = (
            value.cloud_scheduler_invoker_service_account.strip().lower()
        )
        if not scheduler_service_account.endswith(".gserviceaccount.com"):
            errors.append(
                "CLOUD_SCHEDULER_INVOKER_SERVICE_ACCOUNT가 필요합니다."
            )
    if value.single_user_mode_enabled:
        errors.append("SINGLE_USER_MODE_ENABLED는 staging/production 환경에서 사용할 수 없습니다.")
    if errors:
        raise RuntimeError("운영 보안 설정 오류: " + " ".join(errors))


validate_runtime_settings(settings)
