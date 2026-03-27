import json
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.data.models import LandingPageModel, LandingTemplateModel, ScraperConfigModel
from app.schemas import (
    DeployRequest,
    DeployResponse,
    LandingPage,
    LandingTemplate,
    LandingTemplateDetail,
    ScraperConfig,
    TriggerScraperResponse,
    UpdateLandingPageRequest,
)


def _build_public_url(business_topic: str, slug: str, custom_domain: str | None) -> str:
    if custom_domain:
        return f"https://{custom_domain}"

    clean_topic = business_topic.strip().replace(" ", "-").lower()
    if settings.landing_cdn_base_url:
        base = settings.landing_cdn_base_url.rstrip("/")
        return f"{base}/landings/{clean_topic}/{slug}/"

    return f"https://storage.googleapis.com/{settings.client_web_bucket}/landings/{clean_topic}/{slug}/"


def _to_landing_page_schema(model: LandingPageModel) -> LandingPage:
    return LandingPage(
        id=model.id,
        template_id=model.template_id,
        business_topic=model.business_topic,
        business_name=model.business_name,
        slug=model.slug,
        url=model.url,
        status=model.status,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


def _load_template_payload_from_gcs(template_id: str) -> dict:
    try:
        from google.cloud import storage
    except Exception as error:
        raise ValueError("google-cloud-storage 라이브러리를 불러오지 못했습니다.") from error

    bucket_name = settings.site_templates_bucket
    object_path = f"templates/{template_id}.json"

    try:
        blob = storage.Client().bucket(bucket_name).blob(object_path)
        if not blob.exists():
            raise ValueError(f"GCS 템플릿 객체를 찾을 수 없습니다: gs://{bucket_name}/{object_path}")
        return json.loads(blob.download_as_text(encoding="utf-8"))
    except ValueError:
        raise
    except Exception as error:
        raise ValueError("GCS 템플릿을 읽는 중 오류가 발생했습니다.") from error


def _list_templates_from_db(db: Session) -> list[LandingTemplate]:
    rows = db.execute(select(LandingTemplateModel).order_by(LandingTemplateModel.name.asc())).scalars().all()
    return [
        LandingTemplate(
            id=row.id,
            name=row.name,
            description=row.description,
            preview_style=row.preview_style,
        )
        for row in rows
    ]


def list_templates(db: Session) -> list[LandingTemplate]:
    return _list_templates_from_db(db)


def get_template_detail(db: Session, template_id: str) -> LandingTemplateDetail:
    row = (
        db.execute(select(LandingTemplateModel).where(LandingTemplateModel.id == template_id))
        .scalar_one_or_none()
    )
    if row is None:
        raise ValueError("존재하지 않는 템플릿입니다.")

    payload = _load_template_payload_from_gcs(template_id)

    return LandingTemplateDetail(
        id=row.id,
        name=row.name,
        description=row.description,
        preview_style=row.preview_style,
        title=payload.get("title") or "",
        subtitle=payload.get("subtitle") or "",
        body=payload.get("body") or "",
        cta_text=payload.get("cta_text") or "",
        hero_image_url=payload.get("hero_image_url"),
        title_color=payload.get("title_color") or "#0f172a",
        subtitle_color=payload.get("subtitle_color") or "#2563eb",
        body_color=payload.get("body_color") or "#334155",
        cta_text_color=payload.get("cta_text_color") or "#ffffff",
        cta_bg_color=payload.get("cta_bg_color") or "#2563eb",
        background_color=payload.get("background_color") or "#f8fafc",
    )


def create_landing_page(db: Session, request: DeployRequest) -> DeployResponse:
    template_exists = (
        db.execute(select(LandingTemplateModel.id).where(LandingTemplateModel.id == request.template_id)).scalar_one_or_none()
        is not None
    )
    if not template_exists:
        raise ValueError("존재하지 않는 템플릿입니다.")

    already_exists = (
        db.execute(select(LandingPageModel.id).where(LandingPageModel.slug == request.slug)).scalar_one_or_none()
        is not None
    )
    if already_exists:
        raise ValueError("같은 슬러그가 이미 존재합니다.")

    deployed_at = datetime.now(timezone.utc)
    landing_page_id = str(uuid4())
    clean_topic = request.business_topic.strip().replace(" ", "-").lower()
    public_url = _build_public_url(request.business_topic, request.slug, request.custom_domain)

    row = LandingPageModel(
        id=landing_page_id,
        template_id=request.template_id,
        business_topic=request.business_topic,
        business_name=request.business_name,
        slug=request.slug,
        url=public_url,
        status="active",
        custom_domain=request.custom_domain,
        title=request.content.title,
        subtitle=request.content.subtitle,
        body=request.content.body,
        cta_text=request.content.cta_text,
        cta_url=request.content.cta_url,
        hero_image_url=request.content.hero_image_url,
        primary_color=request.content.primary_color,
        secondary_color=request.content.secondary_color,
        background_color=request.content.background_color,
        deployed_at=deployed_at,
    )
    db.add(row)
    db.commit()

    deployment_id = str(uuid4())
    target_path = f"gs://{settings.client_web_bucket}/landings/{clean_topic}/{request.slug}/index.html"

    return DeployResponse(
        deployment_id=deployment_id,
        landing_page_id=landing_page_id,
        target_path=target_path,
        public_url=public_url,
        cdn_enabled=True,
        message="랜딩 페이지가 저장되었습니다. 배포 작업은 API/스크래퍼 컨테이너에서 후속 처리하면 됩니다.",
    )


def list_landing_pages(db: Session) -> list[LandingPage]:
    rows = (
        db.execute(select(LandingPageModel).order_by(LandingPageModel.created_at.desc()))
        .scalars()
        .all()
    )
    return [_to_landing_page_schema(row) for row in rows]


def update_landing_page(db: Session, landing_page_id: str, request: UpdateLandingPageRequest) -> LandingPage:
    row = (
        db.execute(select(LandingPageModel).where(LandingPageModel.id == landing_page_id))
        .scalar_one_or_none()
    )
    if row is None:
        raise ValueError("랜딩 페이지를 찾을 수 없습니다.")

    row.business_topic = request.business_topic
    row.business_name = request.business_name
    row.status = request.status
    row.updated_at = datetime.now(timezone.utc)

    db.commit()
    db.refresh(row)
    return _to_landing_page_schema(row)


def delete_landing_page(db: Session, landing_page_id: str) -> None:
    row = (
        db.execute(select(LandingPageModel).where(LandingPageModel.id == landing_page_id))
        .scalar_one_or_none()
    )
    if row is None:
        raise ValueError("랜딩 페이지를 찾을 수 없습니다.")

    db.delete(row)
    db.commit()


def get_scraper_config(db: Session) -> ScraperConfig:
    row = db.execute(select(ScraperConfigModel).limit(1)).scalar_one()

    emails = [item.strip() for item in row.receiver_emails.split(",") if item.strip()]
    keywords = [item.strip() for item in row.keywords.split(",") if item.strip()]

    return ScraperConfig(
        enabled=row.enabled,
        schedule_mode=row.schedule_mode,
        notify_time=row.notify_time,
        interval_minutes=row.interval_minutes,
        dedup_mode=row.dedup_mode,
        dedup_retention_hours=row.dedup_retention_hours,
        receiver_emails=emails,
        keywords=keywords,
    )


def upsert_scraper_config(db: Session, config: ScraperConfig) -> ScraperConfig:
    row = db.execute(select(ScraperConfigModel).limit(1)).scalar_one()
    row.enabled = config.enabled
    row.schedule_mode = config.schedule_mode
    row.notify_time = config.notify_time
    row.interval_minutes = config.interval_minutes
    row.dedup_mode = config.dedup_mode
    row.dedup_retention_hours = config.dedup_retention_hours
    row.receiver_emails = ",".join(config.receiver_emails)
    row.keywords = ",".join(config.keywords)
    row.updated_at = datetime.now(timezone.utc)
    db.commit()
    return get_scraper_config(db)


def create_scraper_task(config: ScraperConfig, reason: str | None) -> TriggerScraperResponse:
    reason_text = reason or "manual"
    task_id = str(uuid4())
    message = (
        "Scraper 실행 요청이 등록되었습니다. "
        f"mode={config.schedule_mode}, dedup={config.dedup_mode}, receivers={len(config.receiver_emails)}명, reason={reason_text}"
    )
    return TriggerScraperResponse(accepted=True, message=message, task_id=task_id)
