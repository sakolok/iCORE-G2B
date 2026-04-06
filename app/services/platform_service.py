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
        return f"{base}/landings/{clean_topic}/{slug}/index.html"

    return f"https://storage.googleapis.com/{settings.client_web_bucket}/landings/{clean_topic}/{slug}/index.html"


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
    alias_object_path_by_template = {
        "clean-campaign": "templates/template1-clean-campaign.json",
        "dark-product": "templates/template2-dark-product.json",
        "event-highlight": "templates/template3-event-highlight.json",
    }
    candidate_paths = [
        f"templates/{template_id}.json",
        alias_object_path_by_template.get(template_id),
    ]
    candidate_paths = [path for path in candidate_paths if path]

    try:
        bucket = storage.Client().bucket(bucket_name)
        for object_path in candidate_paths:
            blob = bucket.blob(object_path)
            if blob.exists():
                return json.loads(blob.download_as_text(encoding="utf-8"))

        path_message = ", ".join(
            [f"gs://{bucket_name}/{path}" for path in candidate_paths]
        )
        raise ValueError(f"GCS 템플릿 객체를 찾을 수 없습니다: {path_message}")
    except ValueError:
        raise
    except Exception as error:
        raise ValueError("GCS 템플릿을 읽는 중 오류가 발생했습니다.") from error


def _load_template_payload(template_id: str) -> dict:
    return _load_template_payload_from_gcs(template_id)


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


def _build_landing_context(
    request: DeployRequest, hero_image_url: str | None, expires_at: datetime
) -> dict:
    return {
        "title": escape(request.content.title),
        "subtitle": escape(request.content.subtitle),
        "body": escape(request.content.body).replace("\n", "<br>"),
        "cta_text": escape(request.content.cta_text),
        "cta_url": escape(request.content.cta_url),
        "business_name": escape(request.business_name),
        "major": escape(", ".join(request.major_categories) if request.major_categories else "미분류"),
        "minor": escape(", ".join(request.minor_categories) if request.minor_categories else "미분류"),
        "expires_kst": expires_at.astimezone(timezone(timedelta(hours=9))).strftime(
            "%Y-%m-%d %H:%M"
        ),
        "bg": request.content.background_color,
        "primary": request.content.primary_color,
        "secondary": request.content.secondary_color,
        "hero_html": (
            f'<img class="hero-image" src="{escape(hero_image_url)}" alt="landing hero" loading="lazy" />'
            if hero_image_url
            else ""
        ),
    }


def _render_clean_campaign(ctx: dict) -> str:
    return f"""<!doctype html>
<html lang=\"ko\">
<head>
    <meta charset=\"UTF-8\" />
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\" />
    <title>{ctx["title"]}</title>
    <style>
        :root {{ --bg: {ctx["bg"]}; --primary: {ctx["primary"]}; --secondary: {ctx["secondary"]}; }}
        * {{ box-sizing: border-box; }}
        body {{ margin: 0; font-family: "Noto Sans KR", sans-serif; background: #f4f7fb; color: #111827; }}
        .shell {{ max-width: 1140px; margin: 0 auto; padding: 28px 20px 40px; }}
        .mast {{ background: #fff; border-radius: 24px; padding: 26px; border: 1px solid #e5e7eb; }}
        .brand {{ font-size: 30px; font-weight: 800; }}
        .hero {{ margin-top: 24px; display: grid; grid-template-columns: 1.15fr 0.85fr; gap: 24px; align-items: center; }}
        h1 {{ margin: 0 0 8px; font-size: clamp(32px, 5vw, 56px); line-height: 1.08; }}
        h2 {{ margin: 0 0 14px; font-size: clamp(22px, 3.2vw, 34px); color: var(--secondary); }}
        p {{ margin: 0 0 20px; line-height: 1.7; color: #374151; }}
        .cta {{ display: inline-block; text-decoration: none; background: var(--primary); color: #fff; font-weight: 700; padding: 12px 22px; border-radius: 12px; }}
        .meta {{ margin-top: 18px; font-size: 13px; color: #6b7280; }}
        .hero-image {{ width: 100%; border-radius: 16px; border: 1px solid #dbe1ea; box-shadow: 0 20px 50px rgba(2, 6, 23, 0.15); }}
        @media (max-width: 920px) {{ .hero {{ grid-template-columns: 1fr; }} .brand {{ font-size: 24px; }} }}
    </style>
</head>
<body>
    <main class=\"shell\">
        <section class=\"mast\">
            <div class=\"brand\">{ctx["business_name"]}</div>
            <div class=\"hero\">
                <div>
                    <h1>{ctx["title"]}</h1>
                    <h2>{ctx["subtitle"]}</h2>
                    <p>{ctx["body"]}</p>
                    <a class=\"cta\" href=\"{ctx["cta_url"]}\">{ctx["cta_text"]}</a>
                    <div class=\"meta\">대분류: {ctx["major"]} | 소분류: {ctx["minor"]} | 접근 만료 예정: {ctx["expires_kst"]} (KST)</div>
                </div>
                <div>{ctx["hero_html"]}</div>
            </div>
        </section>
    </main>
</body>
</html>
"""


def _render_dark_product(ctx: dict) -> str:
    return f"""<!doctype html>
<html lang=\"ko\">
<head>
    <meta charset=\"UTF-8\" />
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\" />
    <title>{ctx["title"]}</title>
    <style>
        :root {{ --bg: {ctx["bg"]}; --primary: {ctx["primary"]}; --secondary: {ctx["secondary"]}; }}
        * {{ box-sizing: border-box; }}
        body {{
            margin: 0;
            font-family: "Noto Sans KR", sans-serif;
            color: #e5e7eb;
            background:
                radial-gradient(circle at 0% 0%, rgba(59,130,246,0.25), transparent 35%),
                radial-gradient(circle at 100% 0%, rgba(16,185,129,0.2), transparent 30%),
                #030712;
        }}
        .shell {{ max-width: 1200px; margin: 0 auto; padding: 34px 22px 48px; }}
        .top {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 26px; }}
        .brand {{ font-size: 26px; font-weight: 700; letter-spacing: .03em; }}
        .ghost {{ border: 1px solid #374151; color: #d1d5db; padding: 10px 16px; border-radius: 999px; text-decoration: none; }}
        .panel {{ border-radius: 24px; padding: 28px; background: linear-gradient(150deg, rgba(17,24,39,0.9), rgba(15,23,42,0.74)); border: 1px solid rgba(255,255,255,0.08); }}
        .hero {{ display: grid; grid-template-columns: 1fr 1fr; gap: 22px; align-items: stretch; }}
        h1 {{ margin: 0 0 10px; font-size: clamp(34px, 5vw, 60px); }}
        h2 {{ margin: 0 0 14px; color: var(--secondary); font-size: clamp(22px, 3.2vw, 36px); }}
        p {{ margin: 0 0 20px; color: #9ca3af; line-height: 1.8; }}
        .cta {{ display: inline-block; padding: 14px 28px; background: var(--primary); color: #fff; font-weight: 800; text-decoration: none; border-radius: 999px; }}
        .stats {{ margin-top: 20px; display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; }}
        .stat {{ background: rgba(17,24,39,0.9); border: 1px solid rgba(255,255,255,0.08); border-radius: 12px; padding: 10px; font-size: 13px; color: #cbd5e1; }}
        .hero-image {{ width: 100%; height: 100%; min-height: 280px; object-fit: cover; border-radius: 16px; border: 1px solid rgba(255,255,255,0.1); }}
        @media (max-width: 920px) {{ .hero {{ grid-template-columns: 1fr; }} .stats {{ grid-template-columns: 1fr; }} }}
    </style>
</head>
<body>
    <div class=\"shell\">
        <div class=\"top\">
            <div class=\"brand\">{ctx["business_name"]}</div>
            <a class=\"ghost\" href=\"{ctx["cta_url"]}\">문의하기</a>
        </div>
        <section class=\"panel\">
            <div class=\"hero\">
                <article>
                    <h1>{ctx["title"]}</h1>
                    <h2>{ctx["subtitle"]}</h2>
                    <p>{ctx["body"]}</p>
                    <a class=\"cta\" href=\"{ctx["cta_url"]}\">{ctx["cta_text"]}</a>
                    <div class=\"stats\">
                        <div class=\"stat\">대분류<br>{ctx["major"]}</div>
                        <div class=\"stat\">소분류<br>{ctx["minor"]}</div>
                        <div class=\"stat\">만료<br>{ctx["expires_kst"]} KST</div>
                    </div>
                </article>
                <div>{ctx["hero_html"]}</div>
            </div>
        </section>
    </div>
</body>
</html>
"""


def _render_event_highlight(ctx: dict) -> str:
    return f"""<!doctype html>
<html lang=\"ko\">
<head>
    <meta charset=\"UTF-8\" />
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\" />
    <title>{ctx["title"]}</title>
    <style>
        :root {{ --bg: {ctx["bg"]}; --primary: {ctx["primary"]}; --secondary: {ctx["secondary"]}; }}
        * {{ box-sizing: border-box; }}
        body {{
            margin: 0;
            font-family: "Noto Sans KR", sans-serif;
            color: #111827;
            background:
                linear-gradient(135deg, #fff7ed, #f0f9ff 40%, #fefce8 100%);
            min-height: 100vh;
        }}
        .shell {{ max-width: 980px; margin: 0 auto; padding: 24px 18px 36px; }}
        .poster {{ background: #fff; border: 2px solid #111827; border-radius: 28px; overflow: hidden; box-shadow: 14px 14px 0 #111827; }}
        .head {{ background: var(--primary); color: #fff; padding: 20px 24px; display: flex; justify-content: space-between; align-items: center; }}
        .tag {{ background: #111827; color: #fff; border-radius: 999px; padding: 8px 14px; font-weight: 700; font-size: 13px; }}
        .body {{ padding: 26px 24px 28px; }}
        h1 {{ margin: 0 0 10px; font-size: clamp(30px, 5vw, 52px); line-height: 1.06; }}
        h2 {{ margin: 0 0 12px; color: var(--secondary); font-size: clamp(20px, 3.3vw, 32px); }}
        p {{ margin: 0 0 16px; line-height: 1.8; }}
        .hero-image {{ width: 100%; border: 2px solid #111827; border-radius: 16px; margin: 6px 0 18px; }}
        .foot {{ display: flex; justify-content: space-between; align-items: center; gap: 12px; flex-wrap: wrap; }}
        .cta {{ background: #111827; color: #fff; text-decoration: none; font-weight: 800; padding: 12px 18px; border-radius: 12px; }}
        .meta {{ color: #374151; font-size: 13px; }}
    </style>
</head>
<body>
    <main class=\"shell\">
        <section class=\"poster\">
            <header class=\"head\">
                <strong>{ctx["business_name"]}</strong>
                <span class=\"tag\">EVENT</span>
            </header>
            <div class=\"body\">
                <h1>{ctx["title"]}</h1>
                <h2>{ctx["subtitle"]}</h2>
                <p>{ctx["body"]}</p>
                {ctx["hero_html"]}
                <div class=\"foot\">
                    <a class=\"cta\" href=\"{ctx["cta_url"]}\">{ctx["cta_text"]}</a>
                    <div class=\"meta\">{ctx["major"]} · {ctx["minor"]} · {ctx["expires_kst"]} KST까지</div>
                </div>
            </div>
        </section>
    </main>
</body>
</html>
"""


def _render_landing_html(
    template_id: str, request: DeployRequest, hero_image_url: str | None, expires_at: datetime
) -> str:
    ctx = _build_landing_context(request, hero_image_url, expires_at)
    if template_id == "dark-product":
        return _render_dark_product(ctx)
    if template_id == "event-highlight":
        return _render_event_highlight(ctx)
    return _render_clean_campaign(ctx)


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
    html = _render_landing_html(
        request.template_id,
        request,
        uploaded_hero_image_url,
        expires_at,
    )
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
