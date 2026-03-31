import json
import base64
from html import escape
from datetime import datetime, timezone
from datetime import timedelta
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
    major_categories = [item for item in model.major_categories.split(",") if item]
    minor_categories = [item for item in model.minor_categories.split(",") if item]
    return LandingPage(
        id=model.id,
        template_id=model.template_id,
        business_topic=model.business_topic,
        business_name=model.business_name,
        major_categories=major_categories,
        minor_categories=minor_categories,
        slug=model.slug,
        url=model.url,
        status=model.status,
        retention_days=model.retention_days,
        expires_at=model.expires_at,
        is_visible=model.is_visible,
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


def _fallback_template_payload(template_id: str) -> dict:
        # GCS 템플릿이 아직 준비되지 않았을 때 즉시 사용할 기본 템플릿
        base = {
                "clean-campaign": {
                        "title": "울산의 미래를 코딩하다",
                        "subtitle": "빅테크 AI 인재 양성 프로젝트",
                        "body": "울산 데이터센터 시대를 이끌 실무형 AI/클라우드 교육 과정에 참여하세요.",
                        "cta_text": "지금 신청하기",
                        "hero_image_url": "",
                        "title_color": "#f8fafc",
                        "subtitle_color": "#8b5cf6",
                        "body_color": "#cbd5e1",
                        "cta_text_color": "#ffffff",
                        "cta_bg_color": "#6366f1",
                        "background_color": "#080a2c",
                },
                "dark-product": {
                        "title": "왜 울산 AI 교육인가?",
                        "subtitle": "현업 중심 커리큘럼과 프로젝트 기반 성장",
                        "body": "국내 최고 전문가와 함께 실무 능력을 빠르게 끌어올리는 맞춤형 교육 플랫폼입니다.",
                        "cta_text": "학습 로드맵 보기",
                        "hero_image_url": "",
                        "title_color": "#f8fafc",
                        "subtitle_color": "#a78bfa",
                        "body_color": "#94a3b8",
                        "cta_text_color": "#ffffff",
                        "cta_bg_color": "#7c3aed",
                        "background_color": "#05051f",
                },
                "event-highlight": {
                        "title": "AI가 설계하는 나만의 학습 로드맵",
                        "subtitle": "당신의 성장을 가속할 전문 과정",
                        "body": "당신의 목표를 입력하면 데이터/클라우드/생성형 AI까지 맞춤 학습 경로를 제안합니다.",
                        "cta_text": "학습 나침반 찾기",
                        "hero_image_url": "",
                        "title_color": "#f8fafc",
                        "subtitle_color": "#a78bfa",
                        "body_color": "#94a3b8",
                        "cta_text_color": "#ffffff",
                        "cta_bg_color": "#8b5cf6",
                        "background_color": "#070724",
                },
        }
        return base.get(template_id, base["clean-campaign"])


def _load_template_payload(template_id: str) -> dict:
        try:
                return _load_template_payload_from_gcs(template_id)
        except ValueError:
                return _fallback_template_payload(template_id)


def _guess_ext(mime_type: str | None, file_name: str | None) -> str:
        if file_name and "." in file_name:
                ext = file_name.rsplit(".", maxsplit=1)[1].strip().lower()
                if ext in {"jpg", "jpeg", "png", "webp", "gif"}:
                        return ext

        if not mime_type:
                return "png"

        mime_map = {
                "image/jpeg": "jpg",
                "image/jpg": "jpg",
                "image/png": "png",
                "image/webp": "webp",
                "image/gif": "gif",
        }
        return mime_map.get(mime_type.lower(), "png")


def _upload_bytes_to_gcs(
        *,
        bucket_name: str,
        object_path: str,
        data: bytes,
        content_type: str,
        cache_control: str,
) -> None:
        from google.cloud import storage

        client = storage.Client()
        blob = client.bucket(bucket_name).blob(object_path)
        blob.cache_control = cache_control
        blob.upload_from_string(data, content_type=content_type)


def _upload_hero_image_if_needed(request: DeployRequest, clean_topic: str) -> str | None:
        if request.content.hero_image_url:
                return request.content.hero_image_url

        if not request.content.hero_image_base64:
                return None

        try:
                image_bytes = base64.b64decode(request.content.hero_image_base64)
        except Exception as error:
                raise ValueError("이미지 파일 디코딩에 실패했습니다.") from error

        ext = _guess_ext(request.content.hero_image_mime_type, request.content.hero_image_file_name)
        object_path = f"landings/{clean_topic}/{request.slug}/assets/hero.{ext}"
        content_type = request.content.hero_image_mime_type or f"image/{ext}"
        _upload_bytes_to_gcs(
                bucket_name=settings.client_web_bucket,
                object_path=object_path,
                data=image_bytes,
                content_type=content_type,
                cache_control="public, max-age=31536000, immutable",
        )
        return f"https://storage.googleapis.com/{settings.client_web_bucket}/{object_path}"


def _render_landing_html(request: DeployRequest, hero_image_url: str | None, expires_at: datetime) -> str:
        title = escape(request.content.title)
        subtitle = escape(request.content.subtitle)
        body = escape(request.content.body).replace("\n", "<br>")
        cta_text = escape(request.content.cta_text)
        cta_url = escape(request.content.cta_url)
        major = ", ".join(request.major_categories) if request.major_categories else "미분류"
        minor = ", ".join(request.minor_categories) if request.minor_categories else "미분류"
        expires_kst = expires_at.astimezone(timezone(timedelta(hours=9))).strftime("%Y-%m-%d %H:%M")
        bg = request.content.background_color
        primary = request.content.primary_color
        secondary = request.content.secondary_color

        hero_html = ""
        if hero_image_url:
                hero_html = (
                        f'<img class="hero-image" src="{escape(hero_image_url)}" alt="landing hero" loading="lazy" />'
                )

        return f"""<!doctype html>
<html lang=\"ko\">
<head>
    <meta charset=\"UTF-8\" />
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\" />
    <title>{title}</title>
    <style>
        :root {{
            --bg: {bg};
            --primary: {primary};
            --secondary: {secondary};
            --text: #f8fafc;
            --muted: #cbd5e1;
        }}
        * {{ box-sizing: border-box; }}
        body {{
            margin: 0;
            font-family: Pretendard, "Noto Sans KR", sans-serif;
            color: var(--text);
            background:
                radial-gradient(circle at 10% 20%, rgba(99, 102, 241, 0.3), transparent 30%),
                radial-gradient(circle at 90% 10%, rgba(139, 92, 246, 0.22), transparent 30%),
                var(--bg);
            min-height: 100vh;
        }}
        .shell {{ max-width: 1080px; margin: 0 auto; padding: 22px; }}
        .nav {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 52px; }}
        .brand {{ font-size: 34px; font-weight: 800; }}
        .badge {{
            background: linear-gradient(90deg, #4f46e5, #8b5cf6);
            border-radius: 999px;
            padding: 10px 18px;
            font-weight: 700;
            color: #fff;
            text-decoration: none;
            box-shadow: 0 0 30px rgba(99, 102, 241, 0.5);
        }}
        .hero {{ display: grid; grid-template-columns: 1.1fr 0.9fr; gap: 28px; align-items: center; }}
        .hero h1 {{ margin: 0 0 10px; font-size: clamp(36px, 6vw, 72px); line-height: 1.08; }}
        .hero h2 {{ margin: 0 0 16px; color: var(--secondary); font-size: clamp(26px, 4vw, 56px); }}
        .hero p {{ margin: 0 0 24px; color: var(--muted); font-size: clamp(16px, 2vw, 24px); line-height: 1.7; }}
        .cta {{
            display: inline-block;
            border-radius: 999px;
            background: var(--primary);
            color: #fff;
            text-decoration: none;
            font-weight: 800;
            padding: 14px 30px;
            box-shadow: 0 16px 30px rgba(79, 70, 229, 0.35);
        }}
        .hero-image {{ width: 100%; border-radius: 18px; border: 1px solid rgba(255,255,255,.15); }}
        .meta {{
            margin-top: 26px;
            border-radius: 12px;
            border: 1px solid rgba(255, 255, 255, 0.15);
            background: rgba(15, 23, 42, 0.35);
            padding: 14px;
            color: #cbd5e1;
            font-size: 14px;
        }}
        @media (max-width: 920px) {{
            .nav {{ margin-bottom: 28px; }}
            .brand {{ font-size: 24px; }}
            .hero {{ grid-template-columns: 1fr; }}
        }}
    </style>
</head>
<body>
    <div class=\"shell\">
        <div class=\"nav\">
            <div class=\"brand\">{escape(request.business_name)}</div>
            <a class=\"badge\" href=\"{cta_url}\">{cta_text}</a>
        </div>
        <section class=\"hero\">
            <div>
                <h1>{title}</h1>
                <h2>{subtitle}</h2>
                <p>{body}</p>
                <a class=\"cta\" href=\"{cta_url}\">{cta_text}</a>
                <div class=\"meta\">대분류: {escape(major)} | 소분류: {escape(minor)} | 접근 만료 예정: {expires_kst} (KST)</div>
            </div>
            <div>{hero_html}</div>
        </section>
    </div>
</body>
</html>
"""


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

    payload = _load_template_payload(template_id)

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
    expires_at = deployed_at + timedelta(days=request.retention_days)
    landing_page_id = str(uuid4())
    clean_topic = request.business_topic.strip().replace(" ", "-").lower()
    public_url = _build_public_url(request.business_topic, request.slug, request.custom_domain)

    uploaded_hero_image_url = _upload_hero_image_if_needed(request, clean_topic)
    html = _render_landing_html(request, uploaded_hero_image_url, expires_at)
    object_path = f"landings/{clean_topic}/{request.slug}/index.html"

    try:
        _upload_bytes_to_gcs(
            bucket_name=settings.client_web_bucket,
            object_path=object_path,
            data=html.encode("utf-8"),
            content_type="text/html; charset=utf-8",
            cache_control="no-cache, max-age=0",
        )
    except Exception as error:
        raise ValueError("정적 HTML 업로드에 실패했습니다. GCP 권한/버킷 설정을 확인해주세요.") from error

    row = LandingPageModel(
        id=landing_page_id,
        template_id=request.template_id,
        business_topic=request.business_topic,
        business_name=request.business_name,
        major_categories=",".join(request.major_categories),
        minor_categories=",".join(request.minor_categories),
        slug=request.slug,
        url=public_url,
        status="active",
        retention_days=request.retention_days,
        expires_at=expires_at,
        is_visible=True,
        deleted_at=None,
        custom_domain=request.custom_domain,
        title=request.content.title,
        subtitle=request.content.subtitle,
        body=request.content.body,
        cta_text=request.content.cta_text,
        cta_url=request.content.cta_url,
        hero_image_url=uploaded_hero_image_url,
        primary_color=request.content.primary_color,
        secondary_color=request.content.secondary_color,
        background_color=request.content.background_color,
        deployed_at=deployed_at,
    )
    db.add(row)
    db.commit()

    deployment_id = str(uuid4())
    target_path = f"gs://{settings.client_web_bucket}/{object_path}"

    return DeployResponse(
        deployment_id=deployment_id,
        landing_page_id=landing_page_id,
        target_path=target_path,
        public_url=public_url,
        cdn_enabled=True,
        message="랜딩 페이지 HTML이 즉시 업로드되었습니다.",
    )


def list_landing_pages(db: Session) -> list[LandingPage]:
    now = datetime.now(timezone.utc)
    visible_rows = (
        db.execute(
            select(LandingPageModel).where(
                LandingPageModel.is_visible.is_(True),
                LandingPageModel.expires_at < now,
            )
        )
        .scalars()
        .all()
    )
    for row in visible_rows:
        row.is_visible = False
        row.status = "archived"
        row.deleted_at = now
        row.updated_at = now
    if visible_rows:
        db.commit()

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
    row.major_categories = ",".join(request.major_categories)
    row.minor_categories = ",".join(request.minor_categories)
    row.status = request.status
    row.is_visible = request.status != "archived"
    row.deleted_at = datetime.now(timezone.utc) if request.status == "archived" else None
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

    now = datetime.now(timezone.utc)
    row.is_visible = False
    row.status = "archived"
    row.deleted_at = now
    row.updated_at = now
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
