import json
import base64
import hashlib
import requests
from html import escape
from datetime import datetime, time, timezone
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.data.models import (
    LandingPageModel,
    LandingTemplateModel,
    ScraperConfigModel,
    ScraperNoticeModel,
    ScraperRunModel,
)
from app.schemas import (
    DeployRequest,
    DeployResponse,
    LandingPage,
    LandingTemplate,
    LandingTemplateDetail,
    ScraperConfig,
    ScraperDedupFilterRequest,
    ScraperDedupFilterResponse,
    ScraperNotice,
    ScraperRunReportRequest,
    ScraperRunReportResponse,
    ScraperRunSummary,
    TriggerScraperResponse,
    UpdateLandingPageRequest,
)
from app.services.cloud_scheduler_service import get_scheduler_status, run_scheduler_job_now


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
                b64_data = request.content.hero_image_base64
                if "," in b64_data:
                        b64_data = b64_data.split(",")[1]
                image_bytes = base64.b64decode(b64_data)
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


def _upload_item_image_if_needed(request: DeployRequest, clean_topic: str, base64_str: str | None, index: int, prefix: str) -> str | None:
    if not base64_str:
        return None
    try:
        b64_data = base64_str
        if "," in base64_str:
            b64_data = base64_str.split(",")[1]
        image_bytes = base64.b64decode(b64_data)
    except Exception as error:
        raise ValueError(f"{prefix} 첨부 이미지 디코딩에 실패했습니다.") from error

    object_path = f"landings/{clean_topic}/{request.slug}/assets/{prefix}_{index}.png"
    _upload_bytes_to_gcs(
        bucket_name=settings.client_web_bucket,
        object_path=object_path,
        data=image_bytes,
        content_type="image/png",
        cache_control="public, max-age=31536000, immutable",
    )
    return f"https://storage.googleapis.com/{settings.client_web_bucket}/{object_path}"


def _build_landing_context(
    request: DeployRequest, hero_image_url: str | None, expires_at: datetime
) -> dict:
    target_html = "".join([f"<li><span class='chk'>✓</span> {escape(t.description)}</li>" for t in request.content.target_audience])
    features_html = "".join([f"<article class='feature-card'><h3>{escape(f.title)}</h3><p>{escape(f.description)}</p></article>" for f in request.content.features])
    curriculum_html = "".join([f"<div class='step'><div class='step-marker'></div><div class='step-content'><h4>{escape(c.step)}: {escape(c.title)}</h4><p>{escape(c.description)}</p></div></div>" for c in request.content.curriculum])

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
        "target_audience_html": target_html,
        "features_html": features_html,
        "curriculum_html": curriculum_html,
        "content_obj": request.content,
        "hero_image_url_raw": hero_image_url,
    }

def _build_extra_sections_html(ctx: dict, bg_dark: bool = False) -> str:
    """Build stats / infos / faqs HTML + inline CSS for legacy templates."""
    content = ctx.get("content_obj")
    if not content:
        return ""
    parts = []
    text_color = "#e2e8f0" if bg_dark else "#0f172a"
    sub_color = "#9ca3af" if bg_dark else "#64748b"
    card_bg = "rgba(255,255,255,0.05)" if bg_dark else "#f8fafc"
    card_border = "rgba(255,255,255,0.1)" if bg_dark else "#e2e8f0"
    primary = ctx.get("primary", "#2563eb")
    # Stats
    stats = getattr(content, "stats", [])
    if stats:
        cards = "".join([f"<div style='background:{card_bg};border:1px solid {card_border};border-radius:16px;padding:20px;text-align:center'><div style='font-size:32px;font-weight:900;color:{primary}'>{escape(s.value)}</div><div style='font-size:12px;font-weight:700;color:{sub_color};text-transform:uppercase;margin-top:4px'>{escape(s.title)}</div></div>" for s in stats])
        parts.append(f"<div style='display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px;margin:30px 0'>{cards}</div>")
    # Infos
    infos = getattr(content, "infos", [])
    if infos:
        cards = "".join([f"<div style='background:{card_bg};border:1px solid {card_border};border-radius:12px;padding:16px 18px;border-left:4px solid {primary}'><div style='font-size:10px;font-weight:800;color:{primary};text-transform:uppercase;letter-spacing:.15em;margin-bottom:6px'>{escape(i.label)}</div><div style='font-size:15px;font-weight:700;color:{text_color}'>{escape(i.val)}</div></div>" for i in infos])
        parts.append(f"<h3 class='section-title'>모집 정보</h3><div style='display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin-bottom:30px'>{cards}</div>")
    # FAQs
    faqs = getattr(content, "faqs", [])
    if faqs:
        items = "".join([f"<details style='background:{card_bg};border:1px solid {card_border};border-radius:12px;margin-bottom:10px;overflow:hidden'><summary style='padding:16px 20px;font-weight:700;font-size:15px;cursor:pointer;list-style:none;color:{text_color}'>{escape(q.q)}</summary><div style='padding:0 20px 16px;font-size:14px;color:{sub_color};line-height:1.7'>{escape(q.a).replace(chr(10), '<br>')}</div></details>" for q in faqs])
        parts.append(f"<h3 class='section-title'>자주 묻는 질문</h3>{items}")
    return "\n".join(parts)


def _render_clean_campaign(ctx: dict) -> str:
    return f"""<!doctype html>
<html lang="ko">
<head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
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
        p.desc {{ margin: 0 0 20px; line-height: 1.7; color: #374151; font-size: 18px; }}
        .cta {{ display: inline-block; text-decoration: none; background: var(--primary); color: #fff; font-weight: 700; padding: 12px 22px; border-radius: 12px; }}
        .meta {{ margin-top: 18px; font-size: 13px; color: #6b7280; }}
        .hero-image {{ width: 100%; border-radius: 16px; border: 1px solid #dbe1ea; box-shadow: 0 20px 50px rgba(2,6,23,0.15); }}
        .section-title {{ font-size: 28px; margin: 40px 0 20px; font-weight: 800; border-bottom: 2px solid #e5e7eb; padding-bottom: 10px; }}
        .features-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 20px; margin-bottom: 40px; }}
        .feature-card {{ background: #f8fafc; padding: 20px; border-radius: 16px; border: 1px solid #e2e8f0; }}
        .feature-card h3 {{ margin: 0 0 8px; font-size: 18px; color: var(--secondary); }}
        .feature-card p {{ margin: 0; font-size: 15px; color: #475569; line-height: 1.6; }}
        .target-list {{ list-style: none; padding: 0; margin: 0 0 40px; display: grid; gap: 12px; }}
        .target-list li {{ background: #fff; border: 1px solid #e5e7eb; padding: 16px 20px; border-radius: 12px; display: flex; align-items: center; gap: 12px; font-size: 16px; font-weight: 600; box-shadow: 0 2px 4px rgba(0,0,0,0.02); }}
        .target-list .chk {{ color: var(--primary); font-size: 20px; font-weight: bold; }}
        .curriculum-timeline {{ position: relative; padding-left: 24px; margin-bottom: 40px; }}
        .curriculum-timeline::before {{ content: ''; position: absolute; left: 6px; top: 10px; bottom: 10px; width: 2px; background: #e2e8f0; }}
        .step {{ position: relative; margin-bottom: 24px; }}
        .step-marker {{ position: absolute; left: -24px; top: 4px; width: 14px; height: 14px; border-radius: 50%; background: #fff; border: 3px solid var(--primary); z-index: 1; }}
        .step-content h4 {{ margin: 0 0 6px; font-size: 18px; color: #0f172a; }}
        .step-content p {{ margin: 0; font-size: 15px; color: #64748b; line-height: 1.6; }}
        @media (max-width: 920px) {{ .hero {{ grid-template-columns: 1fr; }} .brand {{ font-size: 24px; }} }}
    </style>
</head>
<body>
    <main class="shell">
        <section class="mast">
            <div class="hero">
                <div>
                    <h1>{ctx["title"]}</h1>
                    <h2>{ctx["subtitle"]}</h2>
                    <p class="desc">{ctx["body"]}</p>
                    <a class="cta" href="{ctx["cta_url"]}">{ctx["cta_text"]}</a>
                </div>
                <div>{ctx["hero_html"]}</div>
            </div>
            
            <div class="rich-content">
                { f"<h3 class='section-title'>추천 대상</h3><ul class='target-list'>{ctx['target_audience_html']}</ul>" if ctx["target_audience_html"] else "" }
                { f"<h3 class='section-title'>과정 특징</h3><div class='features-grid'>{ctx['features_html']}</div>" if ctx["features_html"] else "" }
                { f"<h3 class='section-title'>커리큘럼</h3><div class='curriculum-timeline'>{ctx['curriculum_html']}</div>" if ctx["curriculum_html"] else "" }
                {_build_extra_sections_html(ctx, bg_dark=False)}
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
        .section-title {{ font-size: 28px; margin: 40px 0 20px; font-weight: 800; border-bottom: 2px solid rgba(255,255,255,0.1); padding-bottom: 10px; }}
        .features-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 20px; margin-bottom: 40px; }}
        .feature-card {{ background: rgba(255,255,255,0.03); padding: 20px; border-radius: 16px; border: 1px solid rgba(255,255,255,0.08); }}
        .feature-card h3 {{ margin: 0 0 8px; font-size: 18px; color: var(--secondary); }}
        .feature-card p {{ margin: 0; font-size: 15px; color: #9ca3af; line-height: 1.6; }}
        .target-list {{ list-style: none; padding: 0; margin: 0 0 40px; display: grid; gap: 12px; }}
        .target-list li {{ background: rgba(255,255,255,0.03); border: 1px solid rgba(255,255,255,0.08); padding: 16px 20px; border-radius: 12px; display: flex; align-items: center; gap: 12px; font-size: 16px; font-weight: 600; box-shadow: 0 2px 4px rgba(0,0,0,0.2); }}
        .target-list .chk {{ color: var(--primary); font-size: 20px; font-weight: bold; }}
        .curriculum-timeline {{ position: relative; padding-left: 24px; margin-bottom: 40px; }}
        .curriculum-timeline::before {{ content: ''; position: absolute; left: 6px; top: 10px; bottom: 10px; width: 2px; background: rgba(255,255,255,0.1); }}
        .step {{ position: relative; margin-bottom: 24px; }}
        .step-marker {{ position: absolute; left: -24px; top: 4px; width: 14px; height: 14px; border-radius: 50%; background: #030712; border: 3px solid var(--primary); z-index: 1; }}
        .step-content h4 {{ margin: 0 0 6px; font-size: 18px; color: #e2e8f0; }}
        .step-content p {{ margin: 0; font-size: 15px; color: #9ca3af; line-height: 1.6; }}
        @media (max-width: 920px) {{ .hero {{ grid-template-columns: 1fr; }} .stats {{ grid-template-columns: 1fr; }} }}
    </style>
</head>
<body>
    <div class="shell">
        <div class="top">
            <a class="ghost" href="{ctx["cta_url"]}">문의하기</a>
        </div>
        <section class="panel">
            <div class="hero">
                <article>
                    <h1>{ctx["title"]}</h1>
                    <h2>{ctx["subtitle"]}</h2>
                    <p>{ctx["body"]}</p>
                    <a class="cta" href="{ctx["cta_url"]}">{ctx["cta_text"]}</a>
                </article>
                <div>{ctx["hero_html"]}</div>
            </div>
            
            <div class="rich-content">
                { f"<h3 class='section-title'>추천 대상</h3><ul class='target-list'>{ctx['target_audience_html']}</ul>" if ctx["target_audience_html"] else "" }
                { f"<h3 class='section-title'>과정 특징</h3><div class='features-grid'>{ctx['features_html']}</div>" if ctx["features_html"] else "" }
                { f"<h3 class='section-title'>커리큘럼</h3><div class='curriculum-timeline'>{ctx['curriculum_html']}</div>" if ctx["curriculum_html"] else "" }
                {_build_extra_sections_html(ctx, bg_dark=True)}
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
        .section-title {{ font-size: 24px; margin: 30px 0 16px; font-weight: 800; border-bottom: 2px solid #111827; padding-bottom: 8px; }}
        .features-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 16px; margin-bottom: 30px; }}
        .feature-card {{ background: #fff; padding: 16px; border-radius: 12px; border: 2px solid #111827; box-shadow: 4px 4px 0 #111827; }}
        .feature-card h3 {{ margin: 0 0 6px; font-size: 17px; color: var(--secondary); font-weight: 800; }}
        .feature-card p {{ margin: 0; font-size: 14px; line-height: 1.6; }}
        .target-list {{ list-style: none; padding: 0; margin: 0 0 30px; display: grid; gap: 10px; }}
        .target-list li {{ background: #fff; border: 2px solid #111827; padding: 12px 16px; border-radius: 10px; display: flex; align-items: center; gap: 12px; font-size: 15px; font-weight: 700; box-shadow: 4px 4px 0 #111827; }}
        .target-list .chk {{ color: var(--primary); font-size: 18px; font-weight: 900; }}
        .curriculum-timeline {{ position: relative; padding-left: 20px; margin-bottom: 30px; border-left: 3px solid #111827; }}
        .step {{ position: relative; margin-bottom: 20px; padding-left: 14px; }}
        .step-marker {{ position: absolute; left: -21px; top: 4px; width: 14px; height: 14px; border-radius: 50%; background: var(--primary); border: 2px solid #111827; z-index: 1; }}
        .step-content h4 {{ margin: 0 0 4px; font-size: 16px; font-weight: 800; }}
        .step-content p {{ margin: 0; font-size: 14px; line-height: 1.5; }}
        .meta {{ color: #374151; font-size: 13px; }}
    </style>
</head>
<body>
    <main class="shell">
        <section class="poster">
            <header class="head">
                <strong>EVENT</strong>
                <span class="tag">모집중</span>
            </header>
            <div class="body">
                <h1>{ctx["title"]}</h1>
                <h2>{ctx["subtitle"]}</h2>
                <p>{ctx["body"]}</p>
                {ctx["hero_html"]}
                
                <div class="rich-content" style="margin-top:40px;">
                    { f"<h3 class='section-title'>추천 대상</h3><ul class='target-list'>{ctx['target_audience_html']}</ul>" if ctx["target_audience_html"] else "" }
                    { f"<h3 class='section-title'>과정 특징</h3><div class='features-grid'>{ctx['features_html']}</div>" if ctx["features_html"] else "" }
                    { f"<h3 class='section-title'>커리큘럼</h3><div class='curriculum-timeline'>{ctx['curriculum_html']}</div>" if ctx["curriculum_html"] else "" }
                    {_build_extra_sections_html(ctx, bg_dark=False)}
                </div>
                
                <div class="foot" style="margin-top:20px;">
                    <a class="cta" href="{ctx["cta_url"]}">{ctx["cta_text"]}</a>
                </div>
            </div>
        </section>
    </main>
</body>
</html>
"""


def _render_premium_bootcamp(ctx: dict) -> str:
    content = ctx["content_obj"]

    # ── Stats cards ──
    stats_html = ""
    for s in getattr(content, "stats", []):
        stats_html += f"<div class='stat-card'><h3>{escape(s.value)}</h3><p>{escape(s.title)}</p></div>"

    # ── Info cards ──
    infos_html = ""
    for i in getattr(content, "infos", []):
        infos_html += f"<div class='info-card'><span class='info-label'>{escape(i.label)}</span><p class='info-val'>{escape(i.val)}</p></div>"

    # ── Feature cards ──
    features_html = ""
    for idx, f in enumerate(getattr(content, "features", [])):
        img_url = escape(f.image_url or "")
        img_block = f"<div class='feat-img'><img src='{img_url}' alt='' loading='lazy'/></div>" if img_url else ""
        features_html += f"<div class='feat-card'>{img_block}<div class='feat-body'><h3>{escape(f.title)}</h3><p>{escape(f.description)}</p></div></div>"

    # ── Curriculum tabs (JS interactive) ──
    curr_tabs = ""
    curr_panels = ""
    for idx, c in enumerate(getattr(content, "curriculum", [])):
        active_cls = " active" if idx == 0 else ""
        curr_tabs += f"<button class='curr-tab{active_cls}' data-idx='{idx}'>{escape(c.step)}</button>"
        bullets = "".join([f"<li>{escape(b.strip())}</li>" for b in c.description.split(chr(10)) if b.strip()])
        display = "block" if idx == 0 else "none"
        curr_panels += f"<div class='curr-panel' data-idx='{idx}' style='display:{display}'><h3>{escape(c.title)}</h3><ul>{bullets}</ul></div>"

    # ── Target audience ──
    target_html = ""
    for t in getattr(content, "target_audience", []):
        target_html += f"<li><span class='chk-icon'>✓</span>{escape(t.description)}</li>"

    # ── FAQ accordion ──
    faqs_html = ""
    for q in getattr(content, "faqs", []):
        answer = escape(q.a).replace(chr(10), "<br>")
        faqs_html += f"<details class='faq-item'><summary>{escape(q.q)}</summary><div class='faq-ans'>{answer}</div></details>"

    # ── Hero image ──
    hero_img = ""
    raw_hero = ctx.get("hero_image_url_raw") or ""
    if raw_hero:
        hero_img = f"<div class='hero-visual'><img src='{escape(raw_hero)}' alt='hero' /></div>"

    return f"""<!doctype html>
<html lang="ko">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1.0"/>
<title>{ctx["title"]}</title>
<link rel="preconnect" href="https://fonts.googleapis.com"/>
<link href="https://fonts.googleapis.com/css2?family=Noto+Sans+KR:wght@400;600;800;900&display=swap" rel="stylesheet"/>
<style>
:root{{--p:{ctx["primary"]};--s:{ctx["secondary"]};--bg:{ctx["bg"]};}}
*{{box-sizing:border-box;margin:0;padding:0;}}
body{{font-family:"Noto Sans KR",system-ui,sans-serif;color:#1e293b;line-height:1.7;background:var(--bg);-webkit-font-smoothing:antialiased;}}
a{{text-decoration:none;color:inherit;}}
img{{max-width:100%;height:auto;display:block;}}
.inner{{max-width:1200px;margin:0 auto;padding:0 40px;}}

/* ── HERO ── */
.hero{{background:linear-gradient(135deg,#0f172a 0%,#1e3a5f 50%,var(--p) 100%);color:#fff;padding:140px 0 100px;position:relative;overflow:hidden;}}
.hero::after{{content:'';position:absolute;bottom:-2px;left:0;width:100%;height:80px;background:var(--bg);clip-path:ellipse(55% 100% at 50% 100%);}}
.hero .inner{{display:grid;grid-template-columns:1.1fr 0.9fr;gap:60px;align-items:center;}}
.hero-title{{font-size:clamp(36px,5vw,60px);font-weight:900;line-height:1.08;margin-bottom:24px;letter-spacing:-0.03em;}}
.hero-desc{{font-size:18px;color:rgba(255,255,255,0.8);margin-bottom:40px;font-weight:500;}}
.hero-cta{{background:#fff;color:var(--p);padding:18px 44px;border-radius:60px;font-size:17px;font-weight:800;display:inline-block;transition:transform .3s,box-shadow .3s;box-shadow:0 8px 30px rgba(0,0,0,0.25);}}
.hero-cta:hover{{transform:translateY(-3px);box-shadow:0 14px 40px rgba(0,0,0,0.35);}}
.hero-visual img{{border-radius:24px;box-shadow:0 20px 60px rgba(0,0,0,0.4);}}

/* ── STATS ── */
.stats{{background:var(--bg);padding:0 0 80px;position:relative;z-index:2;margin-top:-50px;}}
.stats-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:24px;}}
.stat-card{{background:#fff;border:1px solid #e2e8f0;border-radius:20px;padding:32px 24px;text-align:center;box-shadow:0 4px 20px rgba(0,0,0,0.04);transition:transform .3s,box-shadow .3s;}}
.stat-card:hover{{transform:translateY(-6px);box-shadow:0 16px 40px rgba(0,0,0,0.1);}}
.stat-card h3{{font-size:38px;font-weight:900;color:var(--p);margin-bottom:6px;}}
.stat-card p{{font-size:13px;font-weight:700;color:#64748b;text-transform:uppercase;letter-spacing:.1em;}}

/* ── INFOS ── */
.infos{{background:#f1f5f9;padding:96px 0;}}
.infos-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:20px;}}
.info-card{{background:#fff;border-radius:20px;padding:28px 24px;position:relative;overflow:hidden;border:1px solid #e2e8f0;transition:transform .3s;}}
.info-card:hover{{transform:translateY(-4px);}}
.info-card::before{{content:'';position:absolute;top:0;left:0;width:4px;height:100%;background:var(--p);border-radius:0 4px 4px 0;}}
.info-label{{font-size:11px;font-weight:900;color:var(--p);text-transform:uppercase;letter-spacing:.2em;display:block;margin-bottom:10px;}}
.info-val{{font-size:17px;font-weight:800;color:#0f172a;}}

/* ── TARGET AUDIENCE ── */
.targets{{background:#fff;padding:96px 0;}}
.sec-title{{font-size:clamp(28px,3.5vw,40px);font-weight:900;text-align:center;margin-bottom:16px;color:#0f172a;}}
.sec-sub{{text-align:center;color:#64748b;font-size:16px;margin-bottom:56px;font-weight:500;}}
.target-list{{list-style:none;max-width:700px;margin:0 auto;display:grid;gap:14px;}}
.target-list li{{background:#f8fafc;border:1px solid #e2e8f0;border-radius:16px;padding:18px 24px;font-size:16px;font-weight:700;display:flex;align-items:center;gap:14px;transition:border-color .3s;}}
.target-list li:hover{{border-color:var(--p);}}
.chk-icon{{color:var(--p);font-size:20px;font-weight:900;flex-shrink:0;}}

/* ── FEATURES ── */
.features{{background:linear-gradient(180deg,#f8fafc,#eef2ff);padding:96px 0;}}
.feat-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:32px;}}
.feat-card{{background:#fff;border-radius:24px;overflow:hidden;border:1px solid #e2e8f0;transition:transform .4s,box-shadow .4s;}}
.feat-card:hover{{transform:translateY(-8px);box-shadow:0 24px 48px rgba(0,0,0,0.12);}}
.feat-img{{height:220px;overflow:hidden;background:#f1f5f9;}}
.feat-img img{{width:100%;height:100%;object-fit:cover;transition:transform .8s;}}
.feat-card:hover .feat-img img{{transform:scale(1.08);}}
.feat-body{{padding:32px;}}
.feat-body h3{{font-size:20px;font-weight:800;margin-bottom:12px;transition:color .3s;}}
.feat-card:hover .feat-body h3{{color:var(--p);}}
.feat-body p{{color:#64748b;font-size:15px;line-height:1.7;}}

/* ── CURRICULUM TABS ── */
.curriculum{{background:#0f172a;color:#fff;padding:96px 0;}}
.curr-wrap{{display:grid;grid-template-columns:280px 1fr;gap:48px;align-items:start;}}
.curr-tabs{{display:flex;flex-direction:column;gap:8px;}}
.curr-tab{{background:rgba(255,255,255,0.06);border:1px solid rgba(255,255,255,0.1);color:rgba(255,255,255,0.6);padding:18px 24px;border-radius:14px;font-size:16px;font-weight:700;cursor:pointer;text-align:left;transition:all .3s;}}
.curr-tab:hover{{background:rgba(255,255,255,0.1);color:#fff;}}
.curr-tab.active{{background:var(--p);color:#fff;border-color:var(--p);box-shadow:0 8px 24px rgba(37,99,235,0.4);}}
.curr-panel{{background:rgba(255,255,255,0.05);border:1px solid rgba(255,255,255,0.1);border-radius:24px;padding:48px;}}
.curr-panel h3{{font-size:28px;font-weight:900;margin-bottom:28px;}}
.curr-panel ul{{list-style:none;display:grid;gap:16px;}}
.curr-panel li{{display:flex;align-items:flex-start;gap:12px;font-size:16px;color:rgba(255,255,255,0.85);font-weight:500;}}
.curr-panel li::before{{content:'→';color:var(--p);font-weight:900;flex-shrink:0;}}

/* ── FAQS ── */
.faqs{{background:#fff;padding:96px 0;}}
.faq-list{{max-width:820px;margin:0 auto;}}
.faq-item{{border:1px solid #e2e8f0;border-radius:20px;margin-bottom:16px;overflow:hidden;transition:border-color .3s;}}
.faq-item[open]{{border-color:var(--p);}}
.faq-item summary{{padding:24px 28px;font-weight:700;font-size:17px;cursor:pointer;list-style:none;display:flex;justify-content:space-between;align-items:center;}}
.faq-item summary::-webkit-details-marker{{display:none;}}
.faq-item summary::after{{content:'+';width:32px;height:32px;background:#f1f5f9;border-radius:10px;display:flex;align-items:center;justify-content:center;font-size:22px;font-weight:600;transition:all .3s;flex-shrink:0;}}
.faq-item[open] summary::after{{content:'−';background:var(--p);color:#fff;}}
.faq-ans{{padding:0 28px 28px;color:#64748b;font-size:15px;line-height:1.8;}}

/* ── CTA BOTTOM ── */
.cta-bottom{{background:linear-gradient(135deg,var(--p),#7c3aed);padding:80px 0;text-align:center;}}
.cta-bottom h2{{font-size:clamp(28px,4vw,44px);font-weight:900;color:#fff;margin-bottom:32px;}}
.cta-bottom a{{background:#fff;color:var(--p);padding:20px 56px;border-radius:60px;font-size:18px;font-weight:800;display:inline-block;transition:transform .3s;box-shadow:0 8px 30px rgba(0,0,0,0.2);}}
.cta-bottom a:hover{{transform:translateY(-3px);}}

/* ── FOOTER ── */
.footer{{background:#0f172a;color:rgba(255,255,255,0.4);padding:48px 0;text-align:center;font-size:13px;font-weight:600;letter-spacing:.1em;}}

/* ── RESPONSIVE ── */
@media(max-width:992px){{
  .hero .inner{{grid-template-columns:1fr;gap:40px;}}
  .hero{{padding:100px 0 80px;}}
  .curr-wrap{{grid-template-columns:1fr;}}
  .curr-tabs{{flex-direction:row;overflow-x:auto;}}
  .curr-tab{{white-space:nowrap;}}
  .feat-grid{{grid-template-columns:1fr;}}
  .inner{{padding:0 20px;}}
}}
</style>
</head>
<body>

<section class="hero">
  <div class="inner">
    <div>
      <h1 class="hero-title">{ctx["title"]}</h1>
      <p class="hero-desc">{ctx["subtitle"]}<br/>{ctx["body"]}</p>
      <a href="{ctx["cta_url"]}" class="hero-cta">{ctx["cta_text"]}</a>
    </div>
    {hero_img if hero_img else "<div></div>"}
  </div>
</section>

{"<section class='stats'><div class='inner'><div class='stats-grid'>" + stats_html + "</div></div></section>" if stats_html else ""}

{"<section class='infos'><div class='inner'><div class='infos-grid'>" + infos_html + "</div></div></section>" if infos_html else ""}

{"<section class='targets'><div class='inner'><h2 class='sec-title'>이런 분들에게 추천합니다</h2><ul class='target-list'>" + target_html + "</ul></div></section>" if target_html else ""}

{"<section class='features'><div class='inner'><h2 class='sec-title'>과정 특징</h2><div class='feat-grid'>" + features_html + "</div></div></section>" if features_html else ""}

{"<section class='curriculum'><div class='inner'><h2 class='sec-title' style='color:#fff'>커리큘럼</h2><p class='sec-sub' style='color:rgba(255,255,255,0.6)'>단계별로 설계된 실무 중심 교육 과정</p><div class='curr-wrap'><div class='curr-tabs'>" + curr_tabs + "</div><div class='curr-panels'>" + curr_panels + "</div></div></div></section>" if curr_tabs else ""}

{"<section class='faqs'><div class='inner'><h2 class='sec-title'>자주 묻는 질문</h2><p class='sec-sub'>궁금한 점을 빠르게 확인하세요</p><div class='faq-list'>" + faqs_html + "</div></div></section>" if faqs_html else ""}

<section class="cta-bottom">
  <div class="inner">
    <h2>지금 바로 시작하세요</h2>
    <a href="{ctx["cta_url"]}">{ctx["cta_text"]}</a>
  </div>
</section>

<footer class="footer">
  <div class="inner">© 2026 All Rights Reserved.</div>
</footer>

<script>
document.querySelectorAll('.curr-tab').forEach(function(tab){{
  tab.addEventListener('click',function(){{
    document.querySelectorAll('.curr-tab').forEach(function(t){{t.classList.remove('active')}});
    document.querySelectorAll('.curr-panel').forEach(function(p){{p.style.display='none'}});
    tab.classList.add('active');
    var idx=tab.getAttribute('data-idx');
    var panel=document.querySelector('.curr-panel[data-idx="'+idx+'"]');
    if(panel)panel.style.display='block';
  }});
}});
</script>
</body>
</html>"""


def _render_landing_html(
    template_id: str, request: DeployRequest, hero_image_url: str | None, expires_at: datetime
) -> str:
    ctx = _build_landing_context(request, hero_image_url, expires_at)
    if template_id == "template4-premium-bootcamp":
        return _render_premium_bootcamp(ctx)
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
    
    for i, f in enumerate(request.content.features):
        if f.image_base64:
            f.image_url = _upload_item_image_if_needed(request, clean_topic, f.image_base64, i, "feature")
            f.image_base64 = None

    for i, c in enumerate(request.content.curriculum):
        if c.image_base64:
            c.image_url = _upload_item_image_if_needed(request, clean_topic, c.image_base64, i, "curriculum")
            c.image_base64 = None

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
        features_json=json.dumps([c.model_dump() for c in request.content.features], ensure_ascii=False),
        curriculum_json=json.dumps([c.model_dump() for c in request.content.curriculum], ensure_ascii=False),
        target_audience_json=json.dumps([c.model_dump() for c in request.content.target_audience], ensure_ascii=False),
        stats_json=json.dumps([c.model_dump() for c in request.content.stats], ensure_ascii=False),
        infos_json=json.dumps([c.model_dump() for c in request.content.infos], ensure_ascii=False),
        faqs_json=json.dumps([c.model_dump() for c in request.content.faqs], ensure_ascii=False),
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


def _parse_notify_times(raw: object) -> list[time]:
    if isinstance(raw, time):
        return [raw]

    if isinstance(raw, timedelta):
        total_seconds = int(raw.total_seconds()) % (24 * 60 * 60)
        hour = total_seconds // 3600
        minute = (total_seconds % 3600) // 60
        second = total_seconds % 60
        return [time(hour=hour, minute=minute, second=second)]

    if isinstance(raw, list):
        chunks = [str(item).strip() for item in raw]
    else:
        chunks = [item.strip() for item in str(raw or "").split(",")]

    parsed: list[time] = []
    for candidate in chunks:
        if not candidate:
            continue
        try:
            parsed.append(time.fromisoformat(candidate))
        except ValueError:
            continue

    if not parsed:
        parsed = [time(hour=9, minute=0)]

    unique: dict[str, time] = {}
    for item in parsed:
        unique[item.isoformat()] = item
    return [unique[key] for key in sorted(unique.keys())]


def _serialize_notify_times(values: list[time]) -> str:
    unique: dict[str, time] = {}
    for item in values:
        unique[item.isoformat()] = item
    if not unique:
        unique["09:00:00"] = time(hour=9, minute=0)
    return ",".join(sorted(unique.keys()))


def get_scraper_config(db: Session) -> ScraperConfig:
    row = db.execute(select(ScraperConfigModel).limit(1)).scalar_one()

    emails = [item.strip() for item in row.receiver_emails.split(",") if item.strip()]
    keywords = [item.strip() for item in row.keywords.split(",") if item.strip()]
    gsheet_ids = [item.strip() for item in (row.gsheet_ids or "").split(",") if item.strip()]

    config = ScraperConfig(
        enabled=row.enabled,
        notify_times=_parse_notify_times(row.notify_times),
        gsheet_ids=gsheet_ids,
        receiver_emails=emails,
        keywords=keywords,
        recent_runs=list_scraper_runs(db, limit=10),
    )
    config.scheduler_status = get_scheduler_status(config)
    return config


def upsert_scraper_config(db: Session, config: ScraperConfig) -> ScraperConfig:
    row = db.execute(select(ScraperConfigModel).limit(1)).scalar_one()
    row.enabled = config.enabled
    serialized_notify_times = _serialize_notify_times(config.notify_times)
    try:
        row.notify_times = serialized_notify_times
        db.flush()
    except Exception:
        db.rollback()
        row = db.execute(select(ScraperConfigModel).limit(1)).scalar_one()
        row.enabled = config.enabled
        try:
            # Legacy DB compatibility: notify_times가 TIME 타입이면 TEXT로 승격 후 재시도
            db.execute(text("ALTER TABLE scraper_configs MODIFY COLUMN notify_times TEXT NOT NULL"))
            db.flush()
            row.notify_times = serialized_notify_times
            db.flush()
        except Exception:
            # ALTER 권한이 없거나 실패하면 최소한 첫 번째 시각이라도 저장
            db.rollback()
            row = db.execute(select(ScraperConfigModel).limit(1)).scalar_one()
            row.enabled = config.enabled
            row.notify_times = _parse_notify_times(serialized_notify_times)[0]
    row.gsheet_ids = ",".join(item.strip() for item in config.gsheet_ids if item.strip())
    row.receiver_emails = ",".join(str(email) for email in config.receiver_emails)
    row.keywords = ",".join(config.keywords)
    row.updated_at = datetime.now(timezone.utc)
    db.commit()
    return get_scraper_config(db)


def create_scraper_task(config: ScraperConfig, reason: str | None) -> TriggerScraperResponse:
    reason_text = reason or "manual"
    scheduler_run = run_scheduler_job_now(config, reason)
    if scheduler_run is not None:
        return TriggerScraperResponse(
            accepted=True,
            message=(
                "Cloud Scheduler 수동 실행이 요청되었습니다. "
                f"job={scheduler_run['job_name']}, reason={reason_text}"
            ),
            task_id=scheduler_run["job_name"],
        )

    task_id = str(uuid4())
    message = (
        "Scraper 실행 요청이 등록되었습니다. "
        f"notify_times={len(config.notify_times)}개, receivers={len(config.receiver_emails)}명, reason={reason_text}"
    )
    return TriggerScraperResponse(accepted=True, message=message, task_id=task_id)


def _parse_deadline(raw: str) -> datetime | None:
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed
    except Exception:
        return None


def _fetch_g2b_notices(keywords: list[str]) -> list[ScraperNotice]:
    source_url = settings.scraper_private_api_base.strip()
    if not source_url:
        return []

    notices: list[ScraperNotice] = []
    timeout = 20
    for keyword in keywords:
        try:
            response = requests.get(
                source_url,
                params={"keyword": keyword},
                headers={"Accept": "application/json"},
                timeout=timeout,
            )
            response.raise_for_status()
            payload = response.json()
        except Exception:
            continue

        items: list[dict] = []
        if isinstance(payload, list):
            items = [item for item in payload if isinstance(item, dict)]
        elif isinstance(payload, dict):
            for key in ("items", "results", "data"):
                if isinstance(payload.get(key), list):
                    items = [item for item in payload[key] if isinstance(item, dict)]
                    break

        for item in items:
            title = str(item.get("title") or item.get("noticeTitle") or "").strip()
            if not title:
                continue
            notices.append(
                ScraperNotice(
                    notice_id=str(item.get("notice_id") or item.get("noticeId") or item.get("bidNtceNo") or "").strip(),
                    title=title,
                    agency=str(item.get("agency") or item.get("organization") or item.get("ntceInsttNm") or "").strip(),
                    estimated_price=str(item.get("estimated_price") or item.get("estPrice") or item.get("presmptPrce") or "").strip(),
                    published_at=_parse_deadline(
                        str(
                            item.get("published_at")
                            or item.get("created_at")
                            or item.get("rgstDt")
                            or item.get("bidNtceDt")
                            or ""
                        )
                    ),
                    deadline_at=_parse_deadline(
                        str(item.get("deadline_at") or item.get("deadline") or item.get("bidClseDt") or "")
                    ),
                    notice_url=str(item.get("notice_url") or item.get("url") or item.get("link") or "").strip(),
                )
            )
    return notices


def _build_sheets_service():
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
    except Exception:
        return None

    inline_json = ""
    if inline_json:
        account = json.loads(inline_json)
        creds = service_account.Credentials.from_service_account_info(
            account,
            scopes=["https://www.googleapis.com/auth/spreadsheets"],
        )
        return build("sheets", "v4", credentials=creds)

    try:
        return build("sheets", "v4")
    except Exception:
        return None


def _append_notices_to_sheet(config: ScraperConfig, run_id: str, notices: list[ScraperNotice]) -> int:
    sheet_ids = [item.strip() for item in config.gsheet_ids if item.strip()]
    fallback = settings.gsheet_id.strip()
    if not sheet_ids and fallback:
        sheet_ids = [fallback]
    tab_name = settings.gsheet_tab_name.strip() or "나라장터 공고 수집 목록"
    if not sheet_ids or not notices:
        return 0

    service = _build_sheets_service()
    if service is None:
        return 0

    values: list[list[str]] = []
    now_iso = datetime.now(timezone.utc).isoformat()
    for notice in notices:
        values.append(
            [
                now_iso,
                run_id,
                notice.notice_id,
                notice.title,
                notice.agency,
                notice.estimated_price,
                notice.deadline_at.isoformat() if notice.deadline_at else "",
                notice.notice_url,
            ]
        )

    success_count = 0
    for sheet_id in sheet_ids:
        try:
            service.spreadsheets().values().append(
                spreadsheetId=sheet_id,
                range=f"{tab_name}!A:H",
                valueInputOption="RAW",
                insertDataOption="INSERT_ROWS",
                body={"values": values},
            ).execute()
            success_count += len(values)
        except Exception:
            continue

    return success_count


def _trigger_apps_script_mail_webhook(config: ScraperConfig, run_id: str, notices: list[ScraperNotice]) -> bool:
    webhook_url = settings.apps_script_webhook_url.strip()
    if not webhook_url or not notices:
        return False
    try:
        response = requests.post(
            webhook_url,
            timeout=20,
            json={
                "run_id": run_id,
                "receiver_emails": [str(email) for email in config.receiver_emails],
                "sheet_ids": config.gsheet_ids or [settings.gsheet_id],
                "sheet_tab_name": settings.gsheet_tab_name,
                "notice_count": len(notices),
            },
        )
        return 200 <= response.status_code < 300
    except Exception:
        return False


def run_scraper_pipeline(
    db: Session,
    config: ScraperConfig,
    reason: str | None,
) -> TriggerScraperResponse:
    if not config.enabled:
        return TriggerScraperResponse(
            accepted=True,
            message="스크래퍼가 비활성 상태라 실행이 건너뛰어졌습니다.",
            task_id="disabled",
        )

    run_id = str(uuid4())
    notices = _fetch_g2b_notices(config.keywords)
    notice_count = len(notices)
    filtered = filter_new_scraper_notices(
        db,
        ScraperDedupFilterRequest(
            run_id=run_id,
            notices=notices,
        ),
    )
    deduped_count = filtered.filtered_count
    kept_notices = filtered.notices
    sheet_written_count = _append_notices_to_sheet(config, run_id, kept_notices)
    mail_triggered = _trigger_apps_script_mail_webhook(config, run_id, kept_notices)

    status = "success"
    error_message = None
    if kept_notices and sheet_written_count == 0:
        status = "partial"
        error_message = "Google Sheet 기록 실패"

    if kept_notices and not mail_triggered:
        status = "partial" if status == "success" else status
        if error_message:
            error_message += ", Apps Script 메일 트리거 실패"
        else:
            error_message = "Apps Script 메일 트리거 실패"

    record_scraper_run_report(
        db,
        ScraperRunReportRequest(
            run_id=run_id,
            source="api_server",
            status=status,
            keyword_count=len(config.keywords),
            notice_count=notice_count,
            deduped_count=deduped_count,
            email_sent_count=1 if mail_triggered else 0,
            sheet_written_count=sheet_written_count,
            error_message=error_message,
            executed_at=datetime.now(timezone.utc),
            notices=kept_notices,
        ),
    )

    return TriggerScraperResponse(
        accepted=True,
        message=(
            f"스크래퍼 실행 완료: status={status}, notices={notice_count}, "
            f"deduped={deduped_count}, sheet={sheet_written_count}, reason={reason or 'manual'}"
        ),
        task_id=run_id,
    )


def _to_run_summary(row: ScraperRunModel) -> ScraperRunSummary:
    return ScraperRunSummary(
        run_id=row.run_id,
        status=row.status,
        keyword_count=row.keyword_count,
        notice_count=row.notice_count,
        deduped_count=row.deduped_count,
        email_sent_count=row.email_sent_count,
        sheet_written_count=row.sheet_written_count,
        error_message=row.error_message,
        executed_at=row.executed_at,
    )


def list_scraper_runs(db: Session, limit: int = 20) -> list[ScraperRunSummary]:
    safe_limit = max(1, min(limit, 100))
    rows = (
        db.execute(
            select(ScraperRunModel)
            .order_by(ScraperRunModel.executed_at.desc())
            .limit(safe_limit)
        )
        .scalars()
        .all()
    )
    return [_to_run_summary(row) for row in rows]


def _make_dedup_key(notice: ScraperNotice) -> str:
    notice_id = (notice.notice_id or "").strip().lower()
    title = (notice.title or "").strip().lower()
    raw = notice_id or title
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _notice_fields_for_db(notice: ScraperNotice) -> dict[str, str | None]:
    """DB 컬럼 길이에 맞춤. Pydantic 스키마에 max_length가 없는 필드가 길면 commit 시 DB 오류가 난다."""
    return {
        "notice_id": (notice.notice_id or "")[:160],
        "title": (notice.title or "")[:500],
        "agency": ((notice.agency or "")[:240] or None),
        "estimated_price": ((notice.estimated_price or "")[:120] or None),
        "notice_url": ((notice.notice_url or "")[:600] or None),
        "published_at": notice.published_at,
        "deadline_at": notice.deadline_at,
    }


def get_last_scraper_run_time(db: Session) -> datetime | None:
    return _last_notified_at(db)


def _last_notified_at(db: Session) -> datetime | None:
    row = db.execute(
        select(ScraperRunModel)
        .where(ScraperRunModel.status.in_(["success", "partial"]))
        .order_by(ScraperRunModel.executed_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    return row.executed_at if row is not None else None


def filter_new_scraper_notices(
    db: Session,
    payload: ScraperDedupFilterRequest,
) -> ScraperDedupFilterResponse:
    now = datetime.now(timezone.utc)
    since_notified_at = payload.since_notified_at or _last_notified_at(db)
    # offset-naive → KST(UTC+9)로 통일하여 비교 오류 방지
    kst = timezone(timedelta(hours=9))
    if since_notified_at is not None and since_notified_at.tzinfo is None:
        since_notified_at = since_notified_at.replace(tzinfo=kst)
    kept: list[ScraperNotice] = []

    for notice in payload.notices:
        published = notice.published_at
        if published is not None and published.tzinfo is None:
            published = published.replace(tzinfo=kst)
        if since_notified_at is not None and published is not None and published <= since_notified_at:
            continue

        dedup_key = _make_dedup_key(notice)
        existing = db.execute(
            select(ScraperNoticeModel).where(ScraperNoticeModel.dedup_key == dedup_key)
        ).scalar_one_or_none()

        if existing is None:
            fields = _notice_fields_for_db(notice)
            db.add(
                ScraperNoticeModel(
                    dedup_key=dedup_key,
                    notice_id=fields["notice_id"],
                    title=fields["title"],
                    agency=fields["agency"],
                    estimated_price=fields["estimated_price"],
                    published_at=fields["published_at"],
                    deadline_at=fields["deadline_at"],
                    notice_url=fields["notice_url"],
                    first_seen_at=now,
                    last_seen_at=now,
                    last_run_id=payload.run_id,
                )
            )
            # 같은 요청 payload 안에 동일 dedup_key가 두 번 오면, flush 전에는 DB/SELECT에 안 보여
            # 두 번째 행이 또 INSERT 되며 UNIQUE(dedup_key) 위반 → 500. 반드시 flush.
            db.flush()
            kept.append(notice)
            continue

        existing.last_seen_at = now
        existing.last_run_id = payload.run_id

    db.commit()
    input_count = len(payload.notices)
    kept_count = len(kept)
    return ScraperDedupFilterResponse(
        run_id=payload.run_id,
        input_count=input_count,
        kept_count=kept_count,
        filtered_count=input_count - kept_count,
        notices=kept,
    )


def record_scraper_run_report(db: Session, payload: ScraperRunReportRequest) -> ScraperRunReportResponse:
    executed_at = payload.executed_at
    if executed_at.tzinfo is None:
        executed_at = executed_at.replace(tzinfo=timezone.utc)

    row = db.execute(
        select(ScraperRunModel).where(ScraperRunModel.run_id == payload.run_id)
    ).scalar_one_or_none()

    if row is None:
        row = ScraperRunModel(
            run_id=payload.run_id,
            source=payload.source,
            status=payload.status,
            keyword_count=payload.keyword_count,
            notice_count=payload.notice_count,
            deduped_count=payload.deduped_count,
            email_sent_count=payload.email_sent_count,
            sheet_written_count=payload.sheet_written_count,
            error_message=payload.error_message,
            executed_at=executed_at,
        )
        db.add(row)
    else:
        row.source = payload.source
        row.status = payload.status
        row.keyword_count = payload.keyword_count
        row.notice_count = payload.notice_count
        row.deduped_count = payload.deduped_count
        row.email_sent_count = payload.email_sent_count
        row.sheet_written_count = payload.sheet_written_count
        row.error_message = payload.error_message
        row.executed_at = executed_at

    for notice in payload.notices:
        dedup_key = _make_dedup_key(notice)
        existing = db.execute(
            select(ScraperNoticeModel).where(ScraperNoticeModel.dedup_key == dedup_key)
        ).scalar_one_or_none()
        if existing is None:
            fields = _notice_fields_for_db(notice)
            db.add(
                ScraperNoticeModel(
                    dedup_key=dedup_key,
                    notice_id=fields["notice_id"],
                    title=fields["title"],
                    agency=fields["agency"],
                    estimated_price=fields["estimated_price"],
                    published_at=fields["published_at"],
                    deadline_at=fields["deadline_at"],
                    notice_url=fields["notice_url"],
                    first_seen_at=executed_at,
                    last_seen_at=executed_at,
                    last_run_id=payload.run_id,
                )
            )
            db.flush()
        else:
            fields = _notice_fields_for_db(notice)
            existing.notice_id = fields["notice_id"]
            existing.title = fields["title"]
            existing.agency = fields["agency"]
            existing.estimated_price = fields["estimated_price"]
            existing.published_at = fields["published_at"]
            existing.deadline_at = fields["deadline_at"]
            existing.notice_url = fields["notice_url"]
            existing.last_seen_at = executed_at
            existing.last_run_id = payload.run_id

    db.commit()
    return ScraperRunReportResponse(
        success=True,
        message="스크래퍼 실행 결과가 저장되었습니다.",
        run_id=payload.run_id,
    )
