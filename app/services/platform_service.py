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
from app.g2b.bid_notice import (
    canonical_bid_notice_identity,
    clean_optional_text,
    infer_two_stage_bid,
    missing_bid_notice_context_fields,
    parse_g2b_datetime,
    parse_official_amount,
)
from app.g2b.keyword_policy import evaluate_keyword_title, normalize_keywords
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
    template_backgrounds = {
        "clean-campaign": "#eff6ff",
        "dark-product": "#020617",
        "event-highlight": "#0D1117",
    }

    candidate_paths: list[str] = []
    candidate_paths.extend(
        [f"templates/{template_id}.json", alias_object_path_by_template.get(template_id)]
    )
    candidate_paths = [path for path in candidate_paths if path]

    try:
        bucket = storage.Client().bucket(bucket_name)
        for object_path in candidate_paths:
            blob = bucket.blob(object_path)
            if blob.exists():
                payload = json.loads(blob.download_as_text(encoding="utf-8"))
                if template_id in template_backgrounds:
                    payload["background_color"] = template_backgrounds[template_id]
                return payload

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
    request: DeployRequest, hero_image_url: str | None, instructor_image_url: str | None, expires_at: datetime
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
        "target_audience_html": target_html,
        "features_html": features_html,
        "curriculum_html": curriculum_html,
        "content_obj": request.content,
        "hero_image_url_raw": hero_image_url,
        "instructor_name": escape(request.content.instructor_name or ""),
        "instructor_title": escape(request.content.instructor_title or ""),
        "instructor_description": escape(request.content.instructor_description or "").replace("\n", "<br>"),
        "instructor_image_url": instructor_image_url,
        "sticky_cta_text": escape(request.content.sticky_cta_text or ""),
        "sticky_cta_url": escape(request.content.sticky_cta_url or ""),
        "sticky_cta_note": escape(request.content.sticky_cta_note or "").replace("\n", "<br>"),
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


def _build_instructor_section_html(ctx: dict) -> str:
    if not (ctx.get("instructor_name") or ctx.get("instructor_description")):
        return ""
    instructor_image = ""
    raw_instructor = ctx.get("instructor_image_url") or ""
    if raw_instructor:
        instructor_image = f"<div class='instructor-photo'><img src='{escape(raw_instructor)}' alt='Instructor'/></div>"

    return f"""<section class='section alt'><div class='inner'><div class='instructor-card'>{instructor_image}<div class='instructor-copy'><p class='instructor-label'>강사 소개</p><h2>{ctx['instructor_name']}</h2><p class='instructor-title'>{ctx['instructor_title']}</p><p class='instructor-description'>{ctx['instructor_description']}</p></div></div></div></section>"""


def _build_sticky_cta_html(ctx: dict) -> str:
    if not ctx.get("sticky_cta_text") or not ctx.get("sticky_cta_url"):
        return ""
    note = ctx.get("sticky_cta_note") or ""
    return f"""<div class='sticky-cta-modal'><div class='sticky-cta-bar'><div class='sticky-cta-copy'><p>{note}</p></div><a class='sticky-cta-button' href='{ctx['sticky_cta_url']}'>{ctx['sticky_cta_text']}</a></div></div>"""


def _build_footer_html(ctx: dict) -> str:
    """Build a footer with course info, operator, consortium, and contact details."""
    primary = ctx.get("primary", "#2563eb")
    return f"""<footer class='site-footer'>
  <div class='inner'>
    <div class='footer-bottom'>
      <p>© 2026 iCoreE&C INC. ALL RIGHTS RESERVED.</p>
    </div>
  </div>
</footer>"""


def _build_navbar_html() -> str:
    """Build a fixed top-right navigation with 4 section tabs."""
    return """<nav class='section-nav' id='sectionNav'>
  <a href='#section-info' class='section-nav-item' data-target='section-info'>모집정보</a>
  <a href='#section-features' class='section-nav-item' data-target='section-features'>과정 특징</a>
  <a href='#section-curriculum' class='section-nav-item' data-target='section-curriculum'>커리큘럼</a>
  <a href='#section-faq' class='section-nav-item' data-target='section-faq'>FAQ</a>
</nav>"""


def _build_shared_sections(content):
    """Build HTML fragments for stats/infos/features/curriculum/target/faqs."""
    stats_html = ""
    for s in getattr(content, "stats", []):
        raw = s.value.strip()
        stats_html += f"<div class='stat-card'><h3 data-target='{escape(raw)}'>0</h3><p>{escape(s.title)}</p></div>"
    infos_html = ""
    for i in getattr(content, "infos", []):
        infos_html += f"<div class='info-card'><span class='info-label'>{escape(i.label)}</span><p class='info-val'>{escape(i.val)}</p></div>"
    features_html = ""
    for f in getattr(content, "features", []):
        img_url = escape(f.image_url or "")
        img_block = f"<div class='feat-img'><img src='{img_url}' alt='' loading='lazy'/></div>" if img_url else ""
        features_html += f"<div class='feat-card'>{img_block}<div class='feat-body'><h3>{escape(f.title)}</h3><p>{escape(f.description)}</p></div></div>"
    curr_tabs = ""
    curr_panels = ""
    for idx, c in enumerate(getattr(content, "curriculum", [])):
        active_cls = " active" if idx == 0 else ""
        curr_tabs += f"<button class='curr-tab{active_cls}' data-idx='{idx}'>{escape(c.step)}</button>"
        bullets = "".join([f"<li>{escape(b.strip())}</li>" for b in c.description.split(chr(10)) if b.strip()])
        display = "block" if idx == 0 else "none"
        curr_image = ""
        raw_curr_image = getattr(c, "image_url", "") or ""
        if raw_curr_image:
            curr_image = f"<div class='curr-image'><img src='{escape(raw_curr_image)}' alt='Curriculum'/></div>"
        curr_panels += f"<div class='curr-panel' data-idx='{idx}' style='display:{display}'>{curr_image}<h3>{escape(c.title)}</h3><ul>{bullets}</ul></div>"
    target_html = ""
    for t in getattr(content, "target_audience", []):
        target_html += f"<li><span class='chk-icon'>✓</span>{escape(t.description)}</li>"
    faqs_html = ""
    for q in getattr(content, "faqs", []):
        answer = escape(q.a).replace(chr(10), "<br>")
        faqs_html += f"<details class='faq-item'><summary>{escape(q.q)}</summary><div class='faq-ans'>{answer}</div></details>"
    return stats_html, infos_html, features_html, curr_tabs, curr_panels, target_html, faqs_html


_SHARED_JS = """
/* Curriculum tab switching */
document.querySelectorAll('.curr-tab').forEach(function(tab){
  tab.addEventListener('click',function(){
    document.querySelectorAll('.curr-tab').forEach(function(t){t.classList.remove('active')});
    document.querySelectorAll('.curr-panel').forEach(function(p){p.style.display='none'});
    tab.classList.add('active');
    var idx=tab.getAttribute('data-idx');
    var panel=document.querySelector('.curr-panel[data-idx=\"'+idx+'\"]');
    if(panel)panel.style.display='block';
  });
});
/* Stats counter animation */
function animateCounters(){
  document.querySelectorAll('.stat-card h3[data-target]').forEach(function(el){
    if(el.dataset.done)return;
    var raw=el.getAttribute('data-target');
    var m=raw.match(/^([^0-9]*?)(\\d+)(.*?)$/);
    if(!m){el.textContent=raw;el.dataset.done='1';return;}
    var prefix=m[1],target=parseInt(m[2],10),suffix=m[3];
    var duration=1200,start=performance.now();
    function tick(now){
      var p=Math.min((now-start)/duration,1);
      var ease=1-Math.pow(1-p,3);
      el.textContent=prefix+Math.round(target*ease)+suffix;
      if(p<1)requestAnimationFrame(tick);
      else el.dataset.done='1';
    }
    requestAnimationFrame(tick);
  });
}
var statsEl=document.querySelector('.stats,.section');
if(statsEl&&'IntersectionObserver' in window){
  new IntersectionObserver(function(entries,obs){
    entries.forEach(function(e){
      if(e.isIntersecting){animateCounters();obs.unobserve(e.target);}
    });
  },{threshold:0.3}).observe(statsEl);
}else{animateCounters();}

/* Scroll reveal animation */
(function(){
  var revealEls=document.querySelectorAll('.scroll-reveal');
  if(!revealEls.length)return;
  if('IntersectionObserver' in window){
    var obs=new IntersectionObserver(function(entries){
      entries.forEach(function(e){
        if(e.isIntersecting){
          e.target.classList.add('revealed');
          obs.unobserve(e.target);
        }
      });
    },{threshold:0.12,rootMargin:'0px 0px -40px 0px'});
    revealEls.forEach(function(el){obs.observe(el);});
  }else{
    revealEls.forEach(function(el){el.classList.add('revealed');});
  }
})();

/* Section navbar smooth scroll + active highlight */
(function(){
  var navItems=document.querySelectorAll('.section-nav-item');
  if(!navItems.length)return;
  navItems.forEach(function(a){
    a.addEventListener('click',function(e){
      e.preventDefault();
      var target=document.getElementById(a.getAttribute('data-target'));
      if(target)target.scrollIntoView({behavior:'smooth',block:'start'});
    });
  });
  var sectionIds=['section-info','section-features','section-curriculum','section-faq'];
  var sections=sectionIds.map(function(id){return document.getElementById(id);}).filter(Boolean);
  function updateActiveNav(){
    var scrollY=window.scrollY+120;
    var activeId='';
    sections.forEach(function(sec){
      if(sec.offsetTop<=scrollY)activeId=sec.id;
    });
    navItems.forEach(function(a){
      if(a.getAttribute('data-target')===activeId)a.classList.add('active');
      else a.classList.remove('active');
    });
  }
  window.addEventListener('scroll',updateActiveNav,{passive:true});
  updateActiveNav();
  /* Show/hide navbar after hero */
  var nav=document.getElementById('sectionNav');
  if(nav){
    window.addEventListener('scroll',function(){
      if(window.scrollY>300)nav.classList.add('visible');
      else nav.classList.remove('visible');
    },{passive:true});
  }
})();
"""


def _shared_extra_css(dark: bool = False) -> str:
    """Shared CSS for scroll-reveal, section-nav, footer, and updated sticky CTA."""
    nav_bg = "rgba(15,23,42,0.85)" if dark else "rgba(255,255,255,0.92)"
    nav_text = "#e2e8f0" if dark else "#334155"
    nav_active_bg = "#fff" if dark else "var(--p)"
    nav_active_text = "var(--p)" if dark else "#fff"
    nav_border = "rgba(255,255,255,0.1)" if dark else "rgba(0,0,0,0.08)"
    footer_bg = "#0f172a" if dark else "#f8fafc"
    footer_text = "rgba(255,255,255,0.7)" if dark else "#475569"
    footer_head = "#fff" if dark else "#0f172a"
    footer_link = "#93c5fd" if dark else "var(--p)"
    footer_border = "rgba(255,255,255,0.08)" if dark else "#e2e8f0"
    footer_contact_bg = "rgba(255,255,255,0.06)" if dark else "#fff"
    footer_bottom_text = "rgba(255,255,255,0.3)" if dark else "#94a3b8"
    return f"""
/* ── Scroll Reveal ── */
.scroll-reveal{{opacity:0;transform:translateY(40px);transition:opacity .7s cubic-bezier(.22,1,.36,1),transform .7s cubic-bezier(.22,1,.36,1);}}
.scroll-reveal.revealed{{opacity:1;transform:translateY(0);}}
.scroll-reveal.reveal-scale{{transform:scale(0.92);}}
.scroll-reveal.reveal-scale.revealed{{transform:scale(1);}}
.scroll-reveal.reveal-left{{transform:translateX(-40px);}}
.scroll-reveal.reveal-left.revealed{{transform:translateX(0);}}
/* ── Section Nav ── */
html{{scroll-behavior:smooth;}}
.section-nav{{position:fixed;top:24px;right:24px;z-index:1000;display:flex;gap:4px;padding:6px;background:{nav_bg};backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);border:1px solid {nav_border};border-radius:14px;box-shadow:0 8px 32px rgba(0,0,0,0.12);opacity:0;transform:translateY(-16px);transition:opacity .4s,transform .4s;pointer-events:none;}}
.section-nav.visible{{opacity:1;transform:translateY(0);pointer-events:auto;}}
.section-nav-item{{padding:8px 16px;font-size:13px;font-weight:700;color:{nav_text};border-radius:10px;transition:all .25s;white-space:nowrap;}}
.section-nav-item:hover{{background:rgba(0,0,0,0.06);}}
.section-nav-item.active{{background:{nav_active_bg};color:{nav_active_text};box-shadow:0 2px 8px rgba(0,0,0,0.1);}}
/* ── Site Footer ── */
.site-footer{{background:{footer_bg};border-top:1px solid {footer_border};padding:48px 0 32px;}}
.footer-content{{display:flex;gap:40px;flex-wrap:wrap;}}
.footer-info{{flex:1;min-width:280px;}}
.footer-course-name{{font-weight:800;color:{footer_head};font-size:15px;margin-bottom:6px;}}
.footer-org{{color:{footer_text};font-size:13px;line-height:1.8;}}
.footer-contact{{background:{footer_contact_bg};border:1px solid {footer_border};border-radius:14px;padding:20px 24px;min-width:260px;}}
.footer-contact-title{{font-weight:800;color:{footer_head};font-size:14px;margin-bottom:8px;}}
.footer-contact p{{color:{footer_text};font-size:13px;margin:3px 0;line-height:1.7;}}
.footer-contact a{{color:{footer_link};transition:opacity .2s;}}
.footer-contact a:hover{{opacity:0.7;}}
.footer-bottom{{margin-top:32px;padding-top:20px;border-top:1px solid {footer_border};}}
.footer-bottom p{{font-size:11px;font-weight:700;color:{footer_bottom_text};text-transform:uppercase;letter-spacing:.15em;}}
/* ── Updated Sticky CTA (compact) ── */
.sticky-cta-bar{{padding:10px 20px;gap:16px;}}
.sticky-cta-copy{{flex:1;min-width:0;}}
.sticky-cta-copy p{{font-size:14px;line-height:1.5;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}}
.sticky-cta-button{{flex-shrink:0;padding:10px 28px;font-size:15px;}}
@media(max-width:768px){{
  .section-nav{{top:auto;bottom:80px;right:12px;left:12px;justify-content:center;}}
  .footer-content{{flex-direction:column;gap:24px;}}
  .sticky-cta-copy p{{white-space:normal;}}
}}
"""


def _render_clean_campaign(ctx: dict) -> str:
    content = ctx["content_obj"]
    stats_html, infos_html, features_html, curr_tabs, curr_panels, target_html, faqs_html = _build_shared_sections(content)
    feat_cls = "feat-five" if len(getattr(content, "features", [])) == 5 else ""
    hero_img = ""
    raw_hero = ctx.get("hero_image_url_raw") or ""
    if raw_hero:
        hero_img = f"<div class='hero-visual'><img src='{escape(raw_hero)}' alt='hero'/></div>"
    return f"""<!doctype html>
<html lang="ko">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1.0"/>
<title>{ctx["title"]}</title>
<link href="https://fonts.googleapis.com/css2?family=Noto+Sans+KR:wght@400;600;800;900&display=swap" rel="stylesheet"/>
<style>
:root{{--p:{ctx["primary"]};--s:{ctx["secondary"]};}}
*{{box-sizing:border-box;margin:0;padding:0;}}
body{{font-family:"Noto Sans KR",system-ui,sans-serif;color:#1e293b;line-height:1.7;background:#fff;}}
a{{text-decoration:none;color:inherit;}}
img{{max-width:100%;height:auto;display:block;}}
.inner{{max-width:1400px;margin:0 auto;padding:0 32px;}}
.hero{{padding:120px 0 80px;background:#fff;}}
.hero .inner{{display:grid;grid-template-columns:0.9fr 1.1fr;gap:80px;align-items:center;}}
.hero-title{{font-size:clamp(32px,4.5vw,56px);font-weight:900;line-height:1.1;margin-bottom:20px;color:#0f172a;}}
.hero-subtitle{{font-size:20px;font-weight:700;color:var(--p);margin-bottom:12px;}}
.hero-desc{{font-size:17px;color:#64748b;margin-bottom:36px;}}
.hero-cta{{background:var(--p);color:#fff;padding:16px 40px;border-radius:12px;font-size:16px;font-weight:800;display:inline-block;transition:transform .3s;box-shadow:0 6px 20px rgba(37,99,235,0.3);}}
.hero-cta:hover{{transform:translateY(-2px);}}
@keyframes heroFloat{{0%,100%{{transform:translateY(0)}}50%{{transform:translateY(-12px)}}}}
.hero-visual img{{border-radius:20px;box-shadow:0 16px 48px rgba(0,0,0,0.08);animation:heroFloat 4s ease-in-out infinite;}}
.section{{padding:80px 0;}}
.section.alt{{background:#f8fafc;}}
.sec-title{{font-size:clamp(26px,3vw,36px);font-weight:900;text-align:center;margin-bottom:48px;color:#0f172a;}}
.stats-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:20px;}}
.stat-card{{background:#f8fafc;border:1px solid #e2e8f0;border-radius:16px;padding:28px 20px;text-align:center;transition:transform .3s;}}
.stat-card:hover{{transform:translateY(-4px);}}
.stat-card h3{{font-size:36px;font-weight:900;color:var(--p);margin-bottom:4px;}}
.stat-card p{{font-size:12px;font-weight:700;color:#94a3b8;text-transform:uppercase;}}
.infos-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:16px;}}
.info-card{{background:#fff;border:1px solid #e2e8f0;border-radius:14px;padding:24px;border-left:4px solid var(--p);}}
.info-label{{font-size:13px;font-weight:900;color:var(--p);text-transform:uppercase;letter-spacing:.15em;margin-bottom:8px;display:block;}}
.info-val{{font-size:16px;font-weight:800;color:#0f172a;}}
.target-list{{list-style:none;max-width:640px;margin:0 auto;display:grid;gap:12px;}}
.target-list li{{background:#fff;border:1px solid #e2e8f0;border-radius:14px;padding:16px 22px;font-size:15px;font-weight:700;display:flex;align-items:center;gap:12px;}}
.chk-icon{{color:var(--p);font-weight:900;font-size:18px;flex-shrink:0;}}
.feat-grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:24px;}}
.feat-grid.feat-five{{grid-template-columns:repeat(6,1fr);}}
.feat-grid.feat-five .feat-card:nth-child(-n+3){{grid-column:span 2;}}
.feat-grid.feat-five .feat-card:nth-child(4){{grid-column:2/4;}}
.feat-grid.feat-five .feat-card:nth-child(5){{grid-column:4/6;}}
.feat-card{{background:#fff;border:1px solid #e2e8f0;border-radius:20px;overflow:hidden;transition:transform .3s,box-shadow .3s;}}
.feat-card:hover{{transform:translateY(-6px);box-shadow:0 16px 40px rgba(0,0,0,0.08);}}
.feat-img{{height:200px;overflow:hidden;background:#f1f5f9;}}
.feat-img img{{width:100%;height:100%;object-fit:cover;transition:transform .6s;}}
.feat-card:hover .feat-img img{{transform:scale(1.06);}}
.feat-body{{padding:28px;}}
.feat-body h3{{font-size:18px;font-weight:800;margin-bottom:10px;}}
.feat-body p{{color:#64748b;font-size:14px;}}
.curr-wrap{{display:grid;grid-template-columns:240px 1fr;gap:32px;}}
.curr-tabs{{display:flex;flex-direction:column;gap:6px;}}
.curr-tab{{background:#f1f5f9;border:1px solid #e2e8f0;color:#64748b;padding:14px 20px;border-radius:12px;font-size:15px;font-weight:700;cursor:pointer;text-align:left;transition:all .3s;}}
.curr-tab.active{{background:var(--p);color:#fff;border-color:var(--p);}}
.curr-panel{{background:#f8fafc;border:1px solid #e2e8f0;border-radius:20px;padding:40px;}}
.curr-image{{margin-bottom:24px;border-radius:16px;overflow:hidden;}}
.curr-image img{{width:100%;height:auto;display:block;}}
.curr-panel h3{{font-size:24px;font-weight:900;margin-bottom:24px;color:#0f172a;}}
.curr-panel ul{{list-style:none;display:grid;gap:14px;}}
.curr-panel li{{font-size:15px;color:#475569;font-weight:500;padding-left:20px;position:relative;}}
.curr-panel li::before{{content:'→';position:absolute;left:0;color:var(--p);font-weight:900;}}
.faq-list{{max-width:780px;margin:0 auto;}}
.faq-item{{border:1px solid #e2e8f0;border-radius:16px;margin-bottom:12px;overflow:hidden;transition:border-color .3s;}}
.faq-item[open]{{border-color:var(--p);}}
.faq-item summary{{padding:20px 24px;font-weight:700;font-size:16px;cursor:pointer;list-style:none;display:flex;justify-content:space-between;align-items:center;}}
.faq-item summary::-webkit-details-marker{{display:none;}}
.faq-item summary::after{{content:'+';width:28px;height:28px;background:#f1f5f9;border-radius:8px;display:flex;align-items:center;justify-content:center;font-size:20px;flex-shrink:0;}}
.faq-item[open] summary::after{{content:'−';background:var(--p);color:#fff;}}
.faq-ans{{padding:14px 24px 24px;color:#64748b;font-size:14px;line-height:1.8;border-top:1px solid #e5e7eb;margin:0 10px;}}
.sticky-cta-modal{{position:fixed;bottom:24px;left:50%;transform:translateX(-50%);z-index:999;max-width:600px;width:calc(100% - 48px);}}
.sticky-cta-bar{{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:center;gap:12px;padding:12px 22px;background:#fff;border:1px solid #e2e8f0;border-radius:20px;box-shadow:0 20px 55px rgba(0,0,0,0.25);}}
.sticky-cta-copy p{{margin:0;color:#0f172a;font-size:16px;line-height:1.6;}}
.sticky-cta-button{{display:inline-flex;align-items:center;justify-content:center;padding:16px 34px;border-radius:999px;background:var(--p);color:#fff;font-weight:800;box-shadow:0 12px 24px rgba(37,99,235,0.2);}}
.instructor-card{{display:flex;gap:24px;align-items:center;max-width:980px;margin:0 auto;padding:40px;background:#fff;border:1px solid #e2e8f0;border-radius:28px;}}
.instructor-photo{{width:220px;min-width:220px;border-radius:24px;overflow:hidden;box-shadow:0 16px 40px rgba(15,23,42,0.08);}}
.instructor-photo img{{width:100%;height:100%;object-fit:cover;display:block;}}
.instructor-copy{{max-width:680px;}}
.instructor-label{{font-size:13px;font-weight:800;color:var(--p);text-transform:uppercase;letter-spacing:.2em;display:block;margin-bottom:12px;}}
.instructor-title{{font-size:17px;font-weight:700;color:#64748b;margin:12px 0 0;}}
.instructor-description{{font-size:16px;line-height:1.9;color:#475569;}}
.cta-bottom{{background:var(--p);padding:64px 0 0;text-align:center;}}
.cta-bottom h2{{font-size:clamp(24px,3vw,36px);font-weight:900;color:#fff;margin-bottom:28px;}}
.cta-bottom a{{background:#fff;color:var(--p);padding:16px 48px;border-radius:12px;font-size:17px;font-weight:800;display:inline-block;}}
.footer{{background:transparent;border:none;padding:0;text-align:center;font-size:12px;color:#94a3b8;font-weight:600;}}
@media(max-width:768px){{
  .hero .inner,.curr-wrap{{grid-template-columns:1fr;}}
  .feat-grid,.feat-grid.feat-five{{grid-template-columns:1fr;}}
  .feat-grid.feat-five .feat-card{{grid-column:auto;}}
  .curr-tabs{{flex-direction:row;overflow-x:auto;}}
}}
{_shared_extra_css(dark=False)}
</style>
</head>
<body>
{_build_navbar_html()}
<section class="hero"><div class="inner">
  <div><h1 class="hero-title">{ctx["title"]}</h1><p class="hero-subtitle">{ctx["subtitle"]}</p><p class="hero-desc">{ctx["body"]}</p><a href="{ctx["cta_url"]}" class="hero-cta">{ctx["cta_text"]}</a></div>
  {hero_img if hero_img else "<div></div>"}
</div></section>
{"<section class='section'><div class='inner'><div class='stats-grid'>" + stats_html + "</div></div></section>" if stats_html else ""}
{"<section class='section alt scroll-reveal' id='section-info'><div class='inner'><h2 class='sec-title'>모집 정보</h2><div class='infos-grid'>" + infos_html + "</div></div></section>" if infos_html else ""}
{"<section class='section scroll-reveal'><div class='inner'><h2 class='sec-title'>이런 분들에게 추천합니다</h2><ul class='target-list'>" + target_html + "</ul></div></section>" if target_html else ""}
{"<section class='section alt scroll-reveal' id='section-features'><div class='inner'><h2 class='sec-title'>과정 특징</h2><div class='feat-grid " + feat_cls + "'>" + features_html + "</div></div></section>" if features_html else ""}
{_build_instructor_section_html(ctx)}
{"<section class='section scroll-reveal' id='section-curriculum'><div class='inner'><h2 class='sec-title'>커리큘럼</h2><div class='curr-wrap'><div class='curr-tabs'>" + curr_tabs + "</div><div class='curr-panels'>" + curr_panels + "</div></div></div></section>" if curr_tabs else ""}
{"<section class='section alt scroll-reveal' id='section-faq'><div class='inner'><h2 class='sec-title'>자주 묻는 질문</h2><div class='faq-list'>" + faqs_html + "</div></div></section>" if faqs_html else ""}
<section class="cta-bottom"><div class="inner"><h2>지금 바로 시작하세요</h2><a href="{ctx["cta_url"]}">{ctx["cta_text"]}</a></div></section>
{_build_sticky_cta_html(ctx)}
{_build_footer_html(ctx)}
<script>{_SHARED_JS}</script>
</body></html>"""


def _render_dark_product(ctx: dict) -> str:
    content = ctx["content_obj"]
    stats_html, infos_html, features_html, curr_tabs, curr_panels, target_html, faqs_html = _build_shared_sections(content)
    feat_cls = "feat-five" if len(getattr(content, "features", [])) == 5 else ""
    hero_img = ""
    raw_hero = ctx.get("hero_image_url_raw") or ""
    if raw_hero:
        hero_img = f"<div class='hero-visual'><img src='{escape(raw_hero)}' alt='hero'/></div>"
    return f"""<!doctype html>
<html lang="ko">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1.0"/>
<title>{ctx["title"]}</title>
<link href="https://fonts.googleapis.com/css2?family=Noto+Sans+KR:wght@400;600;800;900&display=swap" rel="stylesheet"/>
<style>
:root{{--p:{ctx["primary"]};--s:{ctx["secondary"]};}}
*{{box-sizing:border-box;margin:0;padding:0;}}
body{{font-family:"Noto Sans KR",system-ui,sans-serif;color:#e2e8f0;line-height:1.7;background:#030712;}}
a{{text-decoration:none;color:inherit;}}
img{{max-width:100%;height:auto;display:block;}}
.inner{{max-width:1400px;margin:0 auto;padding:0 32px;}}
.hero{{padding:120px 0 80px;background:radial-gradient(circle at 20% 50%,rgba(59,130,246,0.15),transparent 50%),radial-gradient(circle at 80% 50%,rgba(139,92,246,0.1),transparent 50%),#030712;}}
.hero .inner{{display:grid;grid-template-columns:0.9fr 1.1fr;gap:80px;align-items:center;}}
.hero-title{{font-size:clamp(32px,4.5vw,56px);font-weight:900;line-height:1.1;margin-bottom:20px;color:#fff;}}
.hero-desc{{font-size:17px;color:#94a3b8;margin-bottom:36px;}}
.hero-cta{{background:var(--p);color:#fff;padding:16px 40px;border-radius:999px;font-size:16px;font-weight:800;display:inline-block;transition:all .3s;box-shadow:0 0 30px rgba(59,130,246,0.4);}}
.hero-cta:hover{{box-shadow:0 0 50px rgba(59,130,246,0.6);transform:translateY(-2px);}}
@keyframes heroFloat{{0%,100%{{transform:translateY(0)}}50%{{transform:translateY(-12px)}}}}
.hero-visual img{{border-radius:20px;border:1px solid rgba(255,255,255,0.1);animation:heroFloat 4s ease-in-out infinite;}}
.section{{padding:80px 0;}}
.section.alt{{background:rgba(255,255,255,0.02);border-top:1px solid rgba(255,255,255,0.06);border-bottom:1px solid rgba(255,255,255,0.06);}}
.sec-title{{font-size:clamp(26px,3vw,36px);font-weight:900;text-align:center;margin-bottom:48px;color:#fff;}}
.stats-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:20px;}}
.stat-card{{background:rgba(255,255,255,0.04);border:1px solid rgba(255,255,255,0.08);border-radius:16px;padding:28px 20px;text-align:center;transition:all .3s;backdrop-filter:blur(8px);}}
.stat-card:hover{{border-color:var(--p);transform:translateY(-4px);box-shadow:0 0 30px rgba(59,130,246,0.2);}}
.stat-card h3{{font-size:36px;font-weight:900;color:var(--p);margin-bottom:4px;}}
.stat-card p{{font-size:12px;font-weight:700;color:#64748b;text-transform:uppercase;}}
.infos-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:16px;}}
.info-card{{background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.08);border-radius:14px;padding:24px;border-left:4px solid var(--p);}}
.info-label{{font-size:13px;font-weight:900;color:var(--p);text-transform:uppercase;letter-spacing:.15em;margin-bottom:8px;display:block;}}
.info-val{{font-size:16px;font-weight:800;color:#e2e8f0;}}
.target-list{{list-style:none;max-width:640px;margin:0 auto;display:grid;gap:12px;}}
.target-list li{{background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.08);border-radius:14px;padding:16px 22px;font-size:15px;font-weight:700;display:flex;gap:12px;}}
.chk-icon{{color:var(--p);font-weight:900;font-size:18px;flex-shrink:0;}}
.feat-grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:24px;}}
.feat-grid.feat-five{{grid-template-columns:repeat(6,1fr);}}
.feat-grid.feat-five .feat-card:nth-child(-n+3){{grid-column:span 2;}}
.feat-grid.feat-five .feat-card:nth-child(4){{grid-column:2/4;}}
.feat-grid.feat-five .feat-card:nth-child(5){{grid-column:4/6;}}
.feat-card{{background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.08);border-radius:20px;overflow:hidden;transition:all .4s;}}
.feat-card:hover{{border-color:var(--p);box-shadow:0 0 40px rgba(59,130,246,0.15);transform:translateY(-6px);}}
.feat-img{{height:200px;overflow:hidden;background:rgba(255,255,255,0.02);}}
.feat-img img{{width:100%;height:100%;object-fit:cover;transition:transform .6s;}}
.feat-card:hover .feat-img img{{transform:scale(1.06);}}
.feat-body{{padding:28px;}}
.feat-body h3{{font-size:18px;font-weight:800;margin-bottom:10px;color:#fff;}}
.feat-body p{{color:#94a3b8;font-size:14px;}}
.curr-wrap{{display:grid;grid-template-columns:240px 1fr;gap:32px;}}
.curr-tabs{{display:flex;flex-direction:column;gap:6px;}}
.curr-tab{{background:rgba(255,255,255,0.04);border:1px solid rgba(255,255,255,0.08);color:#94a3b8;padding:14px 20px;border-radius:12px;font-size:15px;font-weight:700;cursor:pointer;text-align:left;transition:all .3s;}}
.curr-tab:hover{{color:#fff;}}
.curr-tab.active{{background:var(--p);color:#fff;border-color:var(--p);box-shadow:0 0 24px rgba(59,130,246,0.3);}}
.curr-panel{{background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.08);border-radius:20px;padding:40px;}}
.curr-image{{margin-bottom:24px;border-radius:16px;overflow:hidden;}}
.curr-image img{{width:100%;height:auto;display:block;}}
.curr-panel h3{{font-size:24px;font-weight:900;margin-bottom:24px;color:#fff;}}
.curr-panel ul{{list-style:none;display:grid;gap:14px;}}
.curr-panel li{{font-size:15px;color:#94a3b8;font-weight:500;padding-left:20px;position:relative;}}
.curr-panel li::before{{content:'→';position:absolute;left:0;color:var(--p);font-weight:900;}}
.faq-list{{max-width:780px;margin:0 auto;}}
.faq-item{{border:1px solid rgba(255,255,255,0.08);border-radius:16px;margin-bottom:12px;overflow:hidden;transition:border-color .3s;}}
.faq-item[open]{{border-color:var(--p);}}
.faq-item summary{{padding:20px 24px;font-weight:700;font-size:16px;cursor:pointer;list-style:none;display:flex;justify-content:space-between;align-items:center;color:#e2e8f0;}}
.faq-item summary::-webkit-details-marker{{display:none;}}
.faq-item summary::after{{content:'+';width:28px;height:28px;background:rgba(255,255,255,0.06);border-radius:8px;display:flex;align-items:center;justify-content:center;font-size:20px;flex-shrink:0;}}
.faq-item[open] summary::after{{content:'−';background:var(--p);color:#fff;}}
.faq-ans{{padding:14px 24px 24px;color:#94a3b8;font-size:14px;line-height:1.8;border-top:1px solid rgba(255,255,255,0.06);margin:0 10px;}}
.sticky-cta-modal{{position:fixed;bottom:24px;left:50%;transform:translateX(-50%);z-index:999;max-width:600px;width:calc(100% - 48px);}}
.sticky-cta-bar{{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:center;gap:12px;padding:12px 22px;background:#1e293b;border:1px solid rgba(255,255,255,0.18);border-radius:20px;box-shadow:0 20px 55px rgba(0,0,0,0.3);}}
.sticky-cta-copy p{{margin:0;color:#e2e8f0;font-size:16px;line-height:1.6;}}
.sticky-cta-button{{display:inline-flex;align-items:center;justify-content:center;padding:16px 34px;border-radius:999px;background:var(--p);color:#fff;font-weight:800;box-shadow:0 12px 24px rgba(37,99,235,0.2);}}
.instructor-card{{display:flex;gap:24px;align-items:center;max-width:980px;margin:0 auto;padding:40px;background:rgba(255,255,255,0.08);border:1px solid rgba(255,255,255,0.14);border-radius:28px;}}
.instructor-photo{{width:220px;min-width:220px;border-radius:24px;overflow:hidden;box-shadow:0 16px 40px rgba(0,0,0,0.16);}}
.instructor-photo img{{width:100%;height:100%;object-fit:cover;display:block;}}
.instructor-copy{{max-width:680px;}}
.instructor-label{{font-size:13px;font-weight:800;color:#7dd3fc;text-transform:uppercase;letter-spacing:.2em;display:block;margin-bottom:12px;}}
.instructor-title{{font-size:17px;font-weight:700;color:#cbd5e1;margin:12px 0 0;}}
.instructor-description{{font-size:16px;line-height:1.9;color:#e2e8f0;}}
.cta-bottom{{background:linear-gradient(135deg,var(--p),#7c3aed);padding:64px 0 0;text-align:center;}}
.cta-bottom h2{{font-size:clamp(24px,3vw,36px);font-weight:900;color:#fff;margin-bottom:28px;}}
.cta-bottom a{{background:#fff;color:var(--p);padding:16px 48px;border-radius:999px;font-size:17px;font-weight:800;display:inline-block;}}
.footer{{background:#030712;border-top:1px solid rgba(255,255,255,0.06);padding:40px 0;text-align:center;font-size:12px;color:#475569;font-weight:600;}}
@media(max-width:768px){{
  .hero .inner,.curr-wrap{{grid-template-columns:1fr;}}
  .feat-grid,.feat-grid.feat-five{{grid-template-columns:1fr;}}
  .feat-grid.feat-five .feat-card{{grid-column:auto;}}
  .curr-tabs{{flex-direction:row;overflow-x:auto;}}
}}
{_shared_extra_css(dark=True)}
</style>
</head>
<body>
{_build_navbar_html()}
<section class="hero"><div class="inner">
  <div><h1 class="hero-title">{ctx["title"]}</h1><p class="hero-subtitle">{ctx["subtitle"]}</p><p class="hero-desc">{ctx["body"]}</p><a href="{ctx["cta_url"]}" class="hero-cta">{ctx["cta_text"]}</a></div>
  {hero_img if hero_img else "<div></div>"}
</div></section>
{"<section class='section'><div class='inner'><div class='stats-grid'>" + stats_html + "</div></div></section>" if stats_html else ""}
{"<section class='section alt scroll-reveal' id='section-info'><div class='inner'><h2 class='sec-title'>모집 정보</h2><div class='infos-grid'>" + infos_html + "</div></div></section>" if infos_html else ""}
{"<section class='section scroll-reveal'><div class='inner'><h2 class='sec-title'>이런 분들에게 추천합니다</h2><ul class='target-list'>" + target_html + "</ul></div></section>" if target_html else ""}
{"<section class='section alt scroll-reveal' id='section-features'><div class='inner'><h2 class='sec-title'>과정 특징</h2><div class='feat-grid " + feat_cls + "'>" + features_html + "</div></div></section>" if features_html else ""}
{_build_instructor_section_html(ctx)}
{"<section class='section scroll-reveal' id='section-curriculum'><div class='inner'><h2 class='sec-title' style='color:#fff'>커리큘럼</h2><div class='curr-wrap'><div class='curr-tabs'>" + curr_tabs + "</div><div class='curr-panels'>" + curr_panels + "</div></div></div></section>" if curr_tabs else ""}
{"<section class='section alt scroll-reveal' id='section-faq'><div class='inner'><h2 class='sec-title'>자주 묻는 질문</h2><div class='faq-list'>" + faqs_html + "</div></div></section>" if faqs_html else ""}
<section class="cta-bottom"><div class="inner"><h2>지금 바로 시작하세요</h2><a href="{ctx["cta_url"]}">{ctx["cta_text"]}</a></div></section>
{_build_sticky_cta_html(ctx)}
{_build_footer_html(ctx)}
<script>{_SHARED_JS}</script>
</body></html>"""


def _render_event_highlight(ctx: dict) -> str:
    content = ctx["content_obj"]
    
    # Hero image
    hero_img = ""
    raw_hero = ctx.get("hero_image_url_raw") or ""
    if raw_hero:
        hero_img = f"<div class='hero-visual'><img src='{escape(raw_hero)}' alt='hero' /></div>"

    # Stats cards
    stats_html = ""
    for s in getattr(content, "stats", []):
        raw = s.value.strip()
        stats_html += f"<div class='stat-item fade-in-up'><h3><span class='num counter' data-target='{escape(raw)}'>0</span></h3><p>{escape(s.title)}</p></div>"

    # Info cards
    infos_html = ""
    for i in getattr(content, "infos", []):
        infos_html += f"<div class='info-item'><div><span>{escape(i.label)}</span><strong>{escape(i.val)}</strong></div></div>"

    # Features
    features_html = ""
    for f in getattr(content, "features", []):
        features_html += f"<div class='solution-card gradient-border-card fade-in-up'><h3>{escape(f.title)}</h3><p>{escape(f.description)}</p></div>"

    # Curriculum
    curr_html = ""
    for idx, c in enumerate(getattr(content, "curriculum", [])):
        bullets = "".join([f"<li>{escape(b.strip())}</li>" for b in c.description.split(chr(10)) if b.strip()])
        curr_html += f"""<div class='timeline-item fade-in-up'>
            <span class='step-label'>STEP {idx+1}</span>
            <h3>{escape(c.step)}</h3>
            <div class='curriculum-accordion'>
                <div class='timeline-result accordion-btn'>
                    <span>▶ {escape(c.title)}</span>
                    <i style="font-size: 12px;">▼</i>
                </div>
                <div class='accordion-content'>
                    <div class='accordion-inner'>
                        <div class='edu-section'>
                            <ul>{bullets}</ul>
                        </div>
                    </div>
                </div>
            </div>
        </div>"""

    # Target audience
    target_html = ""
    for t in getattr(content, "target_audience", []):
        target_html += f"<li>{escape(t.description)}</li>"

    # FAQs
    faqs_html = ""
    for q in getattr(content, "faqs", []):
        answer = escape(q.a).replace(chr(10), "<br>")
        faqs_html += f"<div class='faq-item'><button class='faq-question'>{escape(q.q)} <span class='faq-icon'>+</span></button><div class='faq-answer'><p>{answer}</p></div></div>"

    return f"""<!doctype html>
<html lang="ko">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1.0"/>
<title>{ctx["title"]}</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=Noto+Sans+KR:wght@300;400;500;700;900&display=swap" rel="stylesheet"/>
<style>
:root {{
    --google-blue: #4285F4;
    --google-red: #EA4335;
    --google-yellow: #FBBC04;
    --google-green: #34A853;
    --bg-dark: #0D1117;
    --bg-alt: #13161D;
    --text-primary: #FFFFFF;
    --text-secondary: #B0B8C1;
    --border: rgba(255, 255, 255, 0.1);
    --gradient-google: linear-gradient(90deg, var(--google-blue), var(--google-red), var(--google-yellow), var(--google-green));
}}
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
html {{ scroll-behavior: smooth; }}
body {{ font-family: 'Noto Sans KR', sans-serif; background-color: var(--bg-dark); color: var(--text-primary); line-height: 1.6; overflow-x: hidden; }}
.num {{ font-family: 'Inter', sans-serif; }}
h1, h2, h3, h4, h5, h6 {{ font-weight: 700; line-height: 1.3; }}
h2 {{ font-size: 2.5rem; text-align: center; margin-bottom: 1rem; }}
.section-sub {{ font-size: 1.2rem; color: var(--text-secondary); text-align: center; margin-bottom: 3rem; word-break: keep-all; }}
.container {{ max-width: 1200px; margin: 0 auto; padding: 0 20px; }}
section {{ padding: 100px 0; position: relative; scroll-margin-top: 15vh; }}
section:nth-child(even) {{ background-color: var(--bg-alt); }}

.btn {{ display: inline-flex; align-items: center; justify-content: center; padding: 16px 32px; border-radius: 8px; font-size: 1.1rem; font-weight: 700; text-decoration: none; transition: all 0.3s ease; cursor: pointer; border: none; }}
.btn-primary {{ background-color: var(--google-blue); color: #fff; box-shadow: 0 4px 15px rgba(66, 133, 244, 0.4); }}
.btn-primary:hover {{ transform: translateY(-2px); box-shadow: 0 6px 20px rgba(66, 133, 244, 0.6); background-color: #3b78e7; }}
.btn-outline {{ background-color: transparent; color: #fff; border: 1px solid var(--border); }}
.btn-outline:hover {{ background-color: rgba(255, 255, 255, 0.05); border-color: rgba(255, 255, 255, 0.2); }}

.gradient-text {{ background: var(--gradient-google); -webkit-background-clip: text; -webkit-text-fill-color: transparent; background-clip: text; }}
.gradient-border-card {{ position: relative; background: var(--bg-dark); border-radius: 16px; padding: 40px 30px; z-index: 1; transition: transform 0.3s ease; height: 100%; }}
.gradient-border-card::before {{ content: ''; position: absolute; inset: -2px; border-radius: 18px; background: var(--gradient-google); z-index: -1; opacity: 0.5; transition: opacity 0.3s ease; }}
.gradient-border-card:hover {{ transform: translateY(-5px); }}
.gradient-border-card:hover::before {{ opacity: 1; }}

.fade-in-up {{ opacity: 0; transform: translateY(50px); transition: opacity 1.3s cubic-bezier(0.16, 1, 0.3, 1), transform 1.3s cubic-bezier(0.16, 1, 0.3, 1); }}
.fade-in-up.visible {{ opacity: 1; transform: translateY(0); }}

.hero {{ min-height: 100vh; display: flex; align-items: center; justify-content: center; padding-top: 150px; background: var(--bg-dark); position: relative; overflow: hidden; text-align: center; }}
.hero-particles {{ position: absolute; top: 0; left: 0; width: 100%; height: 100%; z-index: 0; pointer-events: none; background-image: radial-gradient(circle at 20% 30%, rgba(66, 133, 244, 0.25) 0%, transparent 60%), radial-gradient(circle at 80% 20%, rgba(234, 67, 53, 0.2) 0%, transparent 60%), radial-gradient(circle at 30% 80%, rgba(251, 188, 4, 0.2) 0%, transparent 60%), radial-gradient(circle at 70% 80%, rgba(52, 168, 83, 0.2) 0%, transparent 60%); filter: blur(40px); }}
.hero-content {{ position: relative; z-index: 1; max-width: 900px; display: flex; flex-direction: column; align-items: center; }}
.hero h1 {{ font-size: 4rem; font-weight: 900; letter-spacing: -1.5px; margin-bottom: 24px; word-break: keep-all; }}
.hero p.sub {{ font-size: 1.4rem; color: var(--text-secondary); margin-bottom: 40px; line-height: 1.6; word-break: keep-all; }}
.hero-cta {{ display: flex; justify-content: center; gap: 20px; margin-bottom: 60px; }}
.hero-visual img {{ max-width: 600px; width: 100%; border-radius: 20px; box-shadow: 0 10px 40px rgba(0,0,0,0.5); }}

.stats-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 30px; text-align: center; }}
.stat-item h3 {{ font-size: 3.5rem; color: var(--text-primary); margin-bottom: 10px; font-weight: 800; }}
.stat-item h3 span {{ color: var(--google-blue); }}
.stat-item p {{ color: var(--text-secondary); font-size: 1.1rem; }}

.solution-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 30px; }}
.solution-card h3 {{ font-size: 1.5rem; margin: 15px 0; color: #fff; }}
.solution-card p {{ color: var(--text-secondary); line-height: 1.6; }}

.recruit-box {{ background: linear-gradient(135deg, rgba(66, 133, 244, 0.1), rgba(13, 17, 23, 1)); border: 1px solid rgba(66, 133, 244, 0.3); border-radius: 20px; padding: 50px; text-align: center; max-width: 800px; margin: 0 auto; }}
.recruit-info {{ display: grid; grid-template-columns: 1fr 1fr; gap: 20px; text-align: left; }}
.info-item {{ background: rgba(255, 255, 255, 0.05); padding: 16px 20px; border-radius: 12px; display: flex; align-items: center; gap: 12px; }}
.info-item div span {{ display: block; font-size: 0.9rem; color: var(--text-secondary); }}
.info-item div strong {{ font-size: 1.1rem; color: #fff; }}

.target-box {{ background: var(--bg-dark); border: 1px solid var(--border); border-radius: 16px; padding: 40px; max-width: 600px; margin: 0 auto; }}
.target-box h3 {{ margin-bottom: 24px; font-size: 1.4rem; display: flex; align-items: center; gap: 10px; color: var(--google-blue); }}
.target-list {{ list-style: none; }}
.target-list li {{ margin-bottom: 16px; padding-left: 30px; position: relative; color: var(--text-secondary); }}
.target-list li::before {{ content: '✓'; font-weight: 900; color: var(--google-blue); position: absolute; left: 0; top: 2px; }}

.timeline {{ max-width: 800px; margin: 0 auto; position: relative; }}
.timeline::before {{ content: ''; position: absolute; top: 0; bottom: 0; left: 20px; width: 2px; background: var(--border); }}
.timeline-item {{ position: relative; padding-left: 60px; margin-bottom: 40px; }}
.timeline-item:last-child {{ margin-bottom: 0; }}
.timeline-item::before {{ content: ''; position: absolute; left: 12px; top: 5px; width: 18px; height: 18px; border-radius: 50%; background: var(--bg-alt); border: 4px solid var(--google-blue); z-index: 1; }}
.step-label {{ display: inline-block; color: var(--google-blue); font-weight: 700; font-size: 0.9rem; margin-bottom: 8px; letter-spacing: 1px; }}
.timeline-item h3 {{ font-size: 1.4rem; margin-bottom: 12px; color: #fff; }}
.timeline-result {{ background: rgba(255, 255, 255, 0.05); padding: 14px 20px; border-radius: 8px; font-weight: 600; color: var(--google-green); display: flex; justify-content: space-between; align-items: center; cursor: pointer; width: 100%; transition: background 0.3s ease; position: relative; z-index: 2; }}
.timeline-result:hover {{ background: rgba(255, 255, 255, 0.08); }}
.accordion-content {{ max-height: 0; overflow: hidden; transition: max-height 0.4s ease-out, opacity 0.4s ease-out; opacity: 0; }}
.curriculum-accordion.open .accordion-content {{ max-height: 600px; opacity: 1; transition: max-height 0.6s ease-in-out, opacity 0.4s ease-in; }}
.accordion-inner {{ background: rgba(255, 255, 255, 0.02); border: 1px solid rgba(255, 255, 255, 0.05); border-top: none; border-radius: 0 0 8px 8px; padding: 20px; margin-top: -8px; padding-top: 28px; color: var(--text-secondary); font-size: 0.95rem; line-height: 1.6; }}
.edu-section ul {{ padding-left: 20px; margin: 0; }}
.edu-section ul li {{ margin-bottom: 8px; color: var(--text-secondary); }}

.faq-list {{ max-width: 800px; margin: 0 auto; }}
.faq-item {{ border-bottom: 1px solid var(--border); }}
.faq-item:first-child {{ border-top: 1px solid var(--border); }}
.faq-question {{ width: 100%; text-align: left; background: none; border: none; padding: 24px 20px; font-size: 1.1rem; font-weight: 600; color: var(--text-primary); cursor: pointer; display: flex; justify-content: space-between; align-items: center; font-family: inherit; }}
.faq-icon {{ font-size: 1.5rem; color: var(--google-blue); transition: transform 0.3s; display: inline-block; }}
.faq-item.active .faq-icon {{ transform: rotate(45deg); }}
.faq-answer {{ padding: 0 20px; max-height: 0; overflow: hidden; transition: max-height 0.3s ease, padding 0.3s ease; color: var(--text-secondary); }}
.faq-item.active .faq-answer {{ padding: 0 20px 24px; max-height: 300px; }}

.final-cta {{ background: var(--bg-dark); position: relative; text-align: center; padding: 120px 20px; overflow: hidden; }}
.final-cta::before {{ content: ''; position: absolute; top: 0; left: 0; width: 100%; height: 100%; background: radial-gradient(circle at center, rgba(66, 133, 244, 0.15), transparent 70%); z-index: 0; }}
.final-cta .container {{ position: relative; z-index: 1; }}
.final-cta h2 {{ font-size: 3rem; margin-bottom: 20px; color: #fff; }}
.final-cta .btn-primary {{ font-size: 1.4rem; padding: 24px 80px; border-radius: 50px; margin-bottom: 30px; box-shadow: 0 10px 30px rgba(66, 133, 244, 0.4); }}

@media (max-width: 768px) {{
    .hero h1 {{ font-size: 2.5rem; }}
    .stats-grid, .solution-grid, .recruit-info {{ grid-template-columns: 1fr; }}
}}
{_shared_extra_css(dark=True)}
</style>
</head>
<body>
{_build_navbar_html()}

<section class="hero">
    <div class="hero-particles"></div>
    <div class="container hero-content fade-in-up">
        <h1>{ctx["title"]}</h1>
        <p class="sub">{ctx["subtitle"]}<br>{ctx["body"]}</p>
        <div class="hero-cta">
            <a href="{ctx["cta_url"]}" class="btn btn-primary">{ctx["cta_text"]}</a>
        </div>
        {hero_img}
    </div>
</section>

{"<section id='stats'><div class='container'><div class='stats-grid'>" + stats_html + "</div></div></section>" if stats_html else ""}
{"<section id='infos'><div class='container'><div class='recruit-box fade-in-up'><div class='recruit-info'>" + infos_html + "</div></div></div></section>" if infos_html else ""}
{"<section id='features'><div class='container'><h2 class='fade-in-up'>Features</h2><div class='solution-grid'>" + features_html + "</div></div></section>" if features_html else ""}
{"<section id='target'><div class='container'><div class='target-box fade-in-up'><h3>Target Audience</h3><ul class='target-list'>" + target_html + "</ul></div></div></section>" if target_html else ""}
{"<section id='curriculum'><div class='container'><h2 class='fade-in-up'>Curriculum</h2><div class='timeline'>" + curr_html + "</div></div></section>" if curr_html else ""}
{_build_instructor_section_html(ctx)}
{"<section id='faq'><div class='container'><h2 class='fade-in-up'>FAQ</h2><div class='faq-list fade-in-up'>" + faqs_html + "</div></div></section>" if faqs_html else ""}

<section class="final-cta">
    <div class="container fade-in-up">
        <h2>Start Now</h2>
        <a href="{ctx["cta_url"]}" class="btn btn-primary">{ctx["cta_text"]}</a>
    </div>
</section>

{_build_sticky_cta_html(ctx)}
{_build_footer_html(ctx)}

<script>
// FAQ Accordion
document.querySelectorAll('.faq-item').forEach(item => {{
    const btn = item.querySelector('.faq-question');
    btn.addEventListener('click', () => {{
        const isActive = item.classList.contains('active');
        document.querySelectorAll('.faq-item').forEach(i => i.classList.remove('active'));
        if (!isActive) item.classList.add('active');
    }});
}});

// Curriculum Accordion
document.querySelectorAll('.accordion-btn').forEach(btn => {{
    btn.addEventListener('click', () => {{
        const parent = btn.closest('.curriculum-accordion');
        parent.classList.toggle('open');
    }});
}});

// Intersection Observer for animations
const observer = new IntersectionObserver((entries) => {{
    entries.forEach(entry => {{
        if (entry.isIntersecting) {{
            entry.target.classList.add('visible');
            // Counter Animation
            if (entry.target.querySelector('.counter')) {{
                const counters = entry.target.querySelectorAll('.counter');
                counters.forEach(counter => {{
                    if (counter.dataset.done) return;
                    const raw = counter.getAttribute('data-target');
                    const match = raw.match(/^([^0-9]*?)(\\d+)(.*?)$/);
                    if (!match) {{
                        counter.textContent = raw;
                        counter.dataset.done = '1';
                        return;
                    }}
                    const prefix = match[1], target = parseInt(match[2], 10), suffix = match[3];
                    const duration = 1200;
                    const start = performance.now();
                    function update(time) {{
                        const progress = Math.min((time - start) / duration, 1);
                        const current = Math.floor(progress * target);
                        counter.textContent = prefix + current + suffix;
                        if (progress < 1) requestAnimationFrame(update);
                        else counter.textContent = raw;
                    }}
                    requestAnimationFrame(update);
                    counter.dataset.done = '1';
                }});
            }}
            observer.unobserve(entry.target);
        }}
    }});
}});

document.querySelectorAll('.fade-in-up, .stat-item').forEach(el => observer.observe(el));
</script>
</body>
</html>"""


def _render_premium_bootcamp(ctx: dict) -> str:
    content = ctx["content_obj"]

    # ── Stats cards (data-target for counter animation) ──
    stats_html = ""
    for s in getattr(content, "stats", []):
        raw = s.value.strip()
        stats_html += f"<div class='stat-card'><h3 data-target='{escape(raw)}'>0</h3><p>{escape(s.title)}</p></div>"

    # ── Info cards ──
    infos_html = ""
    for i in getattr(content, "infos", []):
        infos_html += f"<div class='info-card'><span class='info-label'>{escape(i.label)}</span><p class='info-val'>{escape(i.val)}</p></div>"

    # ── Feature cards ──
    feat_count = len(getattr(content, "features", []))
    feat_cls = "feat-five" if feat_count == 5 else ""
    features_html = ""
    for f in getattr(content, "features", []):
        img_url = escape(f.image_url or "")
        img_block = f"<div class='feat-img'><img src='{img_url}' alt='' loading='lazy'/></div>" if img_url else ""
        features_html += f"<div class='feat-card'>{img_block}<div class='feat-body'><h3>{escape(f.title)}</h3><p>{escape(f.description)}</p></div></div>"

    # ── Curriculum tabs ──
    curr_tabs = ""
    curr_panels = ""
    for idx, c in enumerate(getattr(content, "curriculum", [])):
        active_cls = " active" if idx == 0 else ""
        curr_tabs += f"<button class='curr-tab{active_cls}' data-idx='{idx}'>{escape(c.step)}</button>"
        bullets = "".join([f"<li>{escape(b.strip())}</li>" for b in c.description.split(chr(10)) if b.strip()])
        display = "block" if idx == 0 else "none"
        curr_image = ""
        raw_curr_image = getattr(c, "image_url", "") or ""
        if raw_curr_image:
            curr_image = f"<div class='curr-image'><img src='{escape(raw_curr_image)}' alt='Curriculum'/></div>"
        curr_panels += f"<div class='curr-panel' data-idx='{idx}' style='display:{display}'>{curr_image}<h3>{escape(c.title)}</h3><ul>{bullets}</ul></div>"

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

/* ── HERO (straight bottom, no curve) ── */
.hero{{background:linear-gradient(135deg,#0f172a 0%,#1e3a5f 50%,var(--p) 100%);color:#fff;padding:140px 0 100px;position:relative;overflow:hidden;border-bottom:4px solid var(--p);}}
.hero .inner{{display:grid;grid-template-columns:1.1fr 0.9fr;gap:60px;align-items:center;}}
.hero-title{{font-size:clamp(36px,5vw,60px);font-weight:900;line-height:1.08;margin-bottom:24px;letter-spacing:-0.03em;}}
.hero-desc{{font-size:18px;color:rgba(255,255,255,0.8);margin-bottom:40px;font-weight:500;}}
.hero-cta{{background:#fff;color:var(--p);padding:18px 44px;border-radius:60px;font-size:17px;font-weight:800;display:inline-block;transition:transform .3s,box-shadow .3s;box-shadow:0 8px 30px rgba(0,0,0,0.25);}}
.hero-cta:hover{{transform:translateY(-3px);box-shadow:0 14px 40px rgba(0,0,0,0.35);}}
/* Floating animation for hero image */
@keyframes heroFloat{{0%,100%{{transform:translateY(0)}}50%{{transform:translateY(-16px)}}}}
.hero-visual img{{border-radius:24px;box-shadow:0 20px 60px rgba(0,0,0,0.4);animation:heroFloat 4s ease-in-out infinite;}}

/* ── STATS (counter animated) ── */
.stats{{background:var(--bg);padding:60px 0 80px;}}
.stats-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:24px;}}
.stat-card{{background:#fff;border:1px solid #e2e8f0;border-radius:20px;padding:32px 24px;text-align:center;box-shadow:0 4px 20px rgba(0,0,0,0.04);transition:transform .3s,box-shadow .3s;}}
.stat-card:hover{{transform:translateY(-6px);box-shadow:0 16px 40px rgba(0,0,0,0.1);}}
.stat-card h3{{font-size:42px;font-weight:900;color:var(--p);margin-bottom:6px;}}
.stat-card p{{font-size:13px;font-weight:700;color:#64748b;text-transform:uppercase;letter-spacing:.1em;}}

/* ── INFOS (bigger title) ── */
.infos{{background:#f1f5f9;padding:96px 0;}}
.infos-title{{font-size:clamp(32px,4vw,48px);font-weight:900;text-align:center;margin-bottom:48px;color:#0f172a;}}
.infos-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:20px;}}
.info-card{{background:#fff;border-radius:20px;padding:28px 24px;position:relative;overflow:hidden;border:1px solid #e2e8f0;transition:transform .3s;}}
.info-card:hover{{transform:translateY(-4px);}}
.info-card::before{{content:'';position:absolute;top:0;left:0;width:4px;height:100%;background:var(--p);border-radius:0 4px 4px 0;}}
.info-label{{font-size:11px;font-weight:900;color:var(--p);text-transform:uppercase;letter-spacing:.2em;display:block;margin-bottom:10px;}}
.info-val{{font-size:17px;font-weight:800;color:#0f172a;}}

/* ── TARGET ── */
.targets{{background:#fff;padding:96px 0;}}
.sec-title{{font-size:clamp(28px,3.5vw,40px);font-weight:900;text-align:center;margin-bottom:16px;color:#0f172a;}}
.sec-sub{{text-align:center;color:#64748b;font-size:16px;margin-bottom:56px;font-weight:500;}}
.target-list{{list-style:none;max-width:700px;margin:0 auto;display:grid;gap:14px;}}
.target-list li{{background:#f8fafc;border:1px solid #e2e8f0;border-radius:16px;padding:18px 24px;font-size:16px;font-weight:700;display:flex;align-items:center;gap:14px;transition:border-color .3s;}}
.target-list li:hover{{border-color:var(--p);}}
.chk-icon{{color:var(--p);font-size:20px;font-weight:900;flex-shrink:0;}}

/* ── FEATURES (supports 3, 5, 6 layouts) ── */
.features{{background:linear-gradient(180deg,#f8fafc,#eef2ff);padding:96px 0;}}
.feat-grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:32px;}}
.feat-grid.feat-five{{grid-template-columns:repeat(6,1fr);}}
.feat-grid.feat-five .feat-card:nth-child(-n+3){{grid-column:span 2;}}
.feat-grid.feat-five .feat-card:nth-child(4){{grid-column:2/4;}}
.feat-grid.feat-five .feat-card:nth-child(5){{grid-column:4/6;}}
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

/* ── FAQS (separator line between Q and A) ── */
.faqs{{background:#fff;padding:96px 0;}}
.faq-list{{max-width:820px;margin:0 auto;}}
.faq-item{{border:1px solid #e2e8f0;border-radius:20px;margin-bottom:16px;overflow:hidden;transition:border-color .3s;}}
.faq-item[open]{{border-color:var(--p);}}
.faq-item summary{{padding:24px 28px;font-weight:700;font-size:17px;cursor:pointer;list-style:none;display:flex;justify-content:space-between;align-items:center;}}
.faq-item summary::-webkit-details-marker{{display:none;}}
.faq-item summary::after{{content:'+';width:32px;height:32px;background:#f1f5f9;border-radius:10px;display:flex;align-items:center;justify-content:center;font-size:22px;font-weight:600;transition:all .3s;flex-shrink:0;}}
.faq-item[open] summary::after{{content:'−';background:var(--p);color:#fff;}}
.faq-ans{{padding:16px 28px 28px;color:#64748b;font-size:15px;line-height:1.8;border-top:1px solid #e5e7eb;margin:0 12px;padding-top:20px;}}

/* ── CTA BOTTOM ── */
.cta-bottom{{background:linear-gradient(135deg,var(--p),#7c3aed);padding:80px 0;text-align:center;}}
.cta-bottom h2{{font-size:clamp(28px,4vw,44px);font-weight:900;color:#fff;margin-bottom:32px;}}
.cta-bottom a{{background:#fff;color:var(--p);padding:20px 56px;border-radius:60px;font-size:18px;font-weight:800;display:inline-block;transition:transform .3s;box-shadow:0 8px 30px rgba(0,0,0,0.2);}}
.cta-bottom a:hover{{transform:translateY(-3px);}}
.sticky-cta-modal{{position:fixed;bottom:24px;left:24px;right:24px;z-index:999;max-width:420px;min-width:320px;width:calc(100% - 48px);}}
.sticky-cta-bar{{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:center;gap:12px;padding:20px 22px;background:linear-gradient(180deg,rgba(255,255,255,0.95),rgba(248,250,252,0.95));border:1px solid rgba(15,23,42,0.08);border-radius:22px;box-shadow:0 16px 40px rgba(15,23,42,0.12);}}
.sticky-cta-copy strong{{display:block;font-size:17px;font-weight:900;color:#0f172a;}}
.sticky-cta-copy p{{margin:8px 0 0;color:#475569;font-size:14px;line-height:1.6;}}
.sticky-cta-button{{display:inline-flex;align-items:center;justify-content:center;padding:16px 34px;border-radius:999px;background:var(--p);color:#fff;font-weight:800;box-shadow:0 12px 24px rgba(37,99,235,0.18);}}
.instructor-card{{display:flex;flex-wrap:wrap;gap:24px;align-items:center;max-width:980px;margin:0 auto;padding:40px;background:#fff;border:1px solid rgba(15,23,42,0.08);border-radius:28px;}}
.instructor-photo{{width:220px;min-width:220px;border-radius:24px;overflow:hidden;box-shadow:0 16px 40px rgba(15,23,42,0.08);}}
.instructor-photo img{{width:100%;height:100%;object-fit:cover;display:block;}}
.instructor-copy{{max-width:680px;}}
.instructor-label{{font-size:13px;font-weight:800;color:#0f172a;text-transform:uppercase;letter-spacing:.2em;display:block;margin-bottom:12px;}}
.instructor-title{{font-size:17px;font-weight:700;color:#64748b;margin:12px 0 0;}}
.instructor-description{{font-size:16px;line-height:1.9;color:#475569;}}

/* ── FOOTER ── */
.footer{{background:#0f172a;color:rgba(255,255,255,0.4);padding:48px 0;text-align:center;font-size:13px;font-weight:600;letter-spacing:.1em;}}

@media(max-width:992px){{
  .hero .inner{{grid-template-columns:1fr;gap:40px;}}
  .hero{{padding:100px 0 80px;}}
  .curr-wrap{{grid-template-columns:1fr;}}
  .curr-tabs{{flex-direction:row;overflow-x:auto;}}
  .curr-tab{{white-space:nowrap;}}
  .feat-grid,.feat-grid.feat-five{{grid-template-columns:1fr;}}
  .feat-grid.feat-five .feat-card{{grid-column:auto;}}
  .inner{{padding:0 20px;}}
}}
{_shared_extra_css(dark=False)}
</style>
</head>
<body>
{_build_navbar_html()}

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

{"<section class='infos scroll-reveal' id='section-info'><div class='inner'><h2 class='infos-title'>모집 정보</h2><div class='infos-grid'>" + infos_html + "</div></div></section>" if infos_html else ""}

{"<section class='targets scroll-reveal'><div class='inner'><h2 class='sec-title'>이런 분들에게 추천합니다</h2><ul class='target-list'>" + target_html + "</ul></div></section>" if target_html else ""}

{"<section class='features scroll-reveal' id='section-features'><div class='inner'><h2 class='sec-title'>과정 특징</h2><div class='feat-grid " + feat_cls + "'>" + features_html + "</div></div></section>" if features_html else ""}

{"<section class='curriculum scroll-reveal' id='section-curriculum'><div class='inner'><h2 class='sec-title' style='color:#fff'>커리큘럼</h2><p class='sec-sub' style='color:rgba(255,255,255,0.6)'>단계별로 설계된 실무 중심 교육 과정</p><div class='curr-wrap'><div class='curr-tabs'>" + curr_tabs + "</div><div class='curr-panels'>" + curr_panels + "</div></div></div></section>" if curr_tabs else ""}

{"<section class='faqs scroll-reveal' id='section-faq'><div class='inner'><h2 class='sec-title'>자주 묻는 질문</h2><p class='sec-sub'>궁금한 점을 빠르게 확인하세요</p><div class='faq-list'>" + faqs_html + "</div></div></section>" if faqs_html else ""}

<section class="cta-bottom">
  <div class="inner">
    <h2>지금 바로 시작하세요</h2>
    <a href="{ctx["cta_url"]}">{ctx["cta_text"]}</a>
  </div>
</section>

{_build_sticky_cta_html(ctx)}
{_build_footer_html(ctx)}

<script>{_SHARED_JS}</script>
</body>
</html>"""


def _render_landing_html(
    template_id: str, request: DeployRequest, hero_image_url: str | None, instructor_image_url: str | None, expires_at: datetime
) -> str:
    ctx = _build_landing_context(request, hero_image_url, instructor_image_url, expires_at)
    if template_id == "clean-campaign":
        return _render_clean_campaign(ctx)
    if template_id == "dark-product":
        return _render_dark_product(ctx)
    if template_id == "event-highlight":
        return _render_event_highlight(ctx)


def _list_templates_from_db(db: Session) -> list[LandingTemplate]:
    rows = (
        db.execute(
            select(LandingTemplateModel)
            .where(LandingTemplateModel.id != "template4-premium-bootcamp")
            .order_by(LandingTemplateModel.name.asc())
        )
        .scalars()
        .all()
    )
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
        cta_url=payload.get("cta_url") or "",
        hero_image_url=payload.get("hero_image_url"),
        sticky_cta_text=payload.get("sticky_cta_text"),
        sticky_cta_url=payload.get("sticky_cta_url"),
        sticky_cta_note=payload.get("sticky_cta_note"),
        instructor_name=payload.get("instructor_name"),
        instructor_title=payload.get("instructor_title"),
        instructor_description=payload.get("instructor_description"),
        instructor_image_url=payload.get("instructor_image_url"),
        features=list(payload.get("features") or []),
        curriculum=list(payload.get("curriculum") or []),
        target_audience=list(payload.get("target_audience") or []),
        stats=list(payload.get("stats") or []),
        infos=list(payload.get("infos") or []),
        faqs=list(payload.get("faqs") or []),
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

    for i, f in enumerate(request.content.features):
        if f.image_base64:
            f.image_url = _upload_item_image_if_needed(request, clean_topic, f.image_base64, i, "feature")
            f.image_base64 = None

    for i, c in enumerate(request.content.curriculum):
        if c.image_base64:
            c.image_url = _upload_item_image_if_needed(request, clean_topic, c.image_base64, i, "curriculum")
            c.image_base64 = None

    hero_image_url = None
    if request.content.hero_image_base64:
        hero_image_url = _upload_item_image_if_needed(request, clean_topic, request.content.hero_image_base64, 0, "hero")
        request.content.hero_image_base64 = None
    if not hero_image_url and request.content.hero_image_url:
        hero_image_url = request.content.hero_image_url.strip() or None

    instructor_image_url = None
    if request.content.instructor_image_base64:
        instructor_image_url = _upload_item_image_if_needed(request, clean_topic, request.content.instructor_image_base64, 0, "instructor")
        request.content.instructor_image_base64 = None
    if not instructor_image_url and request.content.instructor_image_url:
        instructor_image_url = request.content.instructor_image_url.strip() or None

    html = _render_landing_html(
        request.template_id,
        request,
        hero_image_url,
        instructor_image_url,
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
    keywords = normalize_keywords(row.keywords)
    excluded_keywords = normalize_keywords(row.excluded_keywords)
    gsheet_ids = [item.strip() for item in (row.gsheet_ids or "").split(",") if item.strip()]

    config = ScraperConfig(
        enabled=row.enabled,
        notify_times=_parse_notify_times(row.notify_times),
        gsheet_ids=gsheet_ids,
        receiver_emails=emails,
        keywords=keywords,
        excluded_keywords=excluded_keywords,
        recent_runs=list_scraper_runs(db, limit=10),
    )
    config.scheduler_status = get_scheduler_status(config)
    return config


def upsert_scraper_config(db: Session, config: ScraperConfig) -> ScraperConfig:
    result = db.execute(select(ScraperConfigModel).limit(1))
    row = result.scalar_one_or_none()
    if row is None:
        # 데이터가 없는 경우: 새 객체 생성(Insert) 로직
        serialized_notify_times = _serialize_notify_times(config.notify_times)
        row = ScraperConfigModel(
            enabled=config.enabled,
            notify_times=serialized_notify_times,
            gsheet_ids=",".join(item.strip() for item in config.gsheet_ids if item.strip()),
            receiver_emails=",".join(str(email) for email in config.receiver_emails),
            keywords=",".join(normalize_keywords(config.keywords)),
            excluded_keywords=",".join(normalize_keywords(config.excluded_keywords)),
            updated_at=datetime.now(timezone.utc)
        )
        db.add(row)
    else:
        # 데이터가 있는 경우: 기존 객체 수정(Update) 로직
        row.enabled = config.enabled
        serialized_notify_times = _serialize_notify_times(config.notify_times)
        try:
            row.notify_times = serialized_notify_times
            db.flush()
        except Exception:
            db.rollback()
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
                row.enabled = config.enabled
                row.notify_times = _parse_notify_times(serialized_notify_times)[0]
        row.gsheet_ids = ",".join(item.strip() for item in config.gsheet_ids if item.strip())
        row.receiver_emails = ",".join(str(email) for email in config.receiver_emails)
        row.keywords = ",".join(normalize_keywords(config.keywords))
        row.excluded_keywords = ",".join(normalize_keywords(config.excluded_keywords))
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
    return parse_g2b_datetime(raw)


def _fetch_g2b_notices(
    keywords: list[str],
    excluded_keywords: list[str] | None = None,
) -> list[ScraperNotice]:
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
            title = str(
                item.get("title")
                or item.get("noticeTitle")
                or item.get("bidNtceNm")
                or ""
            ).strip()
            if not title:
                continue
            decision = evaluate_keyword_title(title, keywords, excluded_keywords)
            if not decision.keep:
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
                    notice_url=str(
                        item.get("notice_url")
                        or item.get("url")
                        or item.get("link")
                        or item.get("bidNtceDtlUrl")
                        or ""
                    ).strip(),
                    bid_notice_no=clean_optional_text(
                        item.get("bid_notice_no") or item.get("bidNtceNo")
                    ),
                    bid_notice_ord=clean_optional_text(
                        item.get("bid_notice_ord") or item.get("bidNtceOrd")
                    ),
                    business_name=clean_optional_text(
                        item.get("business_name") or item.get("bidNtceNm")
                    ),
                    demand_agency_name=clean_optional_text(
                        item.get("demand_agency_name") or item.get("dminsttNm")
                    ),
                    base_amount=parse_official_amount(
                        item.get("base_amount")
                        if item.get("base_amount") is not None
                        else item.get("bssamt")
                    ),
                    prearranged_price_decision_method=clean_optional_text(
                        item.get("prearranged_price_decision_method")
                        or item.get("prearngPrceDcsnMthdNm")
                    ),
                    proposal_deadline=parse_g2b_datetime(
                        item.get("proposal_deadline") or item.get("bidClseDt")
                    ),
                    region_restriction=clean_optional_text(
                        item.get("region_restriction")
                        or item.get("prtcptPsblRgnNm")
                    ),
                    is_two_stage_bid=infer_two_stage_bid(
                        item.get("is_two_stage_bid"),
                        item.get("bidMethdNm"),
                        item.get("cntrctCnclsMthdNm"),
                        item.get("sucsfbidMthdNm"),
                    ),
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
    notices = _fetch_g2b_notices(config.keywords, config.excluded_keywords)
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
    incomplete_notice_count = sum(
        1 for notice in notices if missing_bid_notice_context_fields(notice)
    )
    if kept_notices and sheet_written_count == 0:
        status = "partial"
        error_message = "Google Sheet 기록 실패"

    if kept_notices and not mail_triggered:
        status = "partial" if status == "success" else status
        if error_message:
            error_message += ", Apps Script 메일 트리거 실패"
        else:
            error_message = "Apps Script 메일 트리거 실패"

    if incomplete_notice_count:
        status = "failed"
        error_message = (
            f"공식 필드가 미완성인 입찰공고가 {incomplete_notice_count}건 있어 "
            "수집 체크포인트를 갱신하지 않았습니다."
        )

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


def _make_legacy_dedup_key(notice: ScraperNotice) -> str:
    notice_id = (notice.notice_id or "").strip().lower()
    title = (notice.title or "").strip().lower()
    raw = notice_id or title
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _make_dedup_key(notice: ScraperNotice) -> str:
    official_identity = canonical_bid_notice_identity(
        notice.bid_notice_no,
        notice.bid_notice_ord,
    )
    if official_identity is not None:
        raw = "bid-notice:" + "|".join(official_identity)
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()
    return _make_legacy_dedup_key(notice)


def _notice_fields_for_db(notice: ScraperNotice) -> dict[str, object | None]:
    """DB 컬럼 길이에 맞춤. Pydantic 스키마에 max_length가 없는 필드가 길면 commit 시 DB 오류가 난다."""
    bid_notice_no = clean_optional_text(notice.bid_notice_no, max_length=160)
    bid_notice_ord = clean_optional_text(notice.bid_notice_ord, max_length=20)
    if bid_notice_no and not bid_notice_ord:
        bid_notice_ord = "00"
    return {
        "notice_id": (notice.notice_id or "")[:160],
        "title": (notice.title or "")[:500],
        "agency": ((notice.agency or "")[:240] or None),
        "estimated_price": ((notice.estimated_price or "")[:120] or None),
        "notice_url": ((notice.notice_url or "")[:600] or None),
        "published_at": notice.published_at,
        "deadline_at": notice.deadline_at,
        "bid_notice_no": bid_notice_no,
        "bid_notice_ord": bid_notice_ord,
        "business_name": clean_optional_text(notice.business_name, max_length=500),
        "demand_agency_name": clean_optional_text(
            notice.demand_agency_name,
            max_length=240,
        ),
        "base_amount": notice.base_amount,
        "prearranged_price_decision_method": clean_optional_text(
            notice.prearranged_price_decision_method,
            max_length=120,
        ),
        "proposal_deadline": notice.proposal_deadline,
        "region_restriction": clean_optional_text(
            notice.region_restriction,
            max_length=240,
        ),
        "is_two_stage_bid": notice.is_two_stage_bid,
    }


def _apply_notice_fields(
    row: ScraperNoticeModel,
    fields: dict[str, object | None],
) -> None:
    for field_name, value in fields.items():
        current_value = getattr(row, field_name, None)
        if value is None and current_value is not None:
            continue
        if (
            isinstance(value, str)
            and not value.strip()
            and isinstance(current_value, str)
            and current_value.strip()
        ):
            continue
        setattr(row, field_name, value)


def _find_existing_scraper_notice(
    db: Session,
    notice: ScraperNotice,
    dedup_key: str,
) -> ScraperNoticeModel | None:
    existing = db.execute(
        select(ScraperNoticeModel).where(ScraperNoticeModel.dedup_key == dedup_key)
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    if canonical_bid_notice_identity(notice.bid_notice_no, notice.bid_notice_ord) is None:
        return None
    legacy_key = _make_legacy_dedup_key(notice)
    if legacy_key == dedup_key:
        return None
    legacy = db.execute(
        select(ScraperNoticeModel).where(
            ScraperNoticeModel.dedup_key == legacy_key,
            ScraperNoticeModel.bid_notice_no.is_(None),
        )
    ).scalar_one_or_none()
    if legacy is not None:
        legacy.dedup_key = dedup_key
    return legacy


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
        is_stale_notice = bool(
            since_notified_at is not None
            and published is not None
            and published <= since_notified_at
        )

        dedup_key = _make_dedup_key(notice)
        existing = _find_existing_scraper_notice(db, notice, dedup_key)

        if existing is None:
            if is_stale_notice:
                continue
            fields = _notice_fields_for_db(notice)
            row = ScraperNoticeModel(
                dedup_key=dedup_key,
                first_seen_at=now,
                last_seen_at=now,
                last_run_id=payload.run_id,
            )
            _apply_notice_fields(row, fields)
            db.add(row)
            # 같은 요청 payload 안에 동일 dedup_key가 두 번 오면, flush 전에는 DB/SELECT에 안 보여
            # 두 번째 행이 또 INSERT 되며 UNIQUE(dedup_key) 위반 → 500. 반드시 flush.
            db.flush()
            kept.append(notice)
            continue

        fields = _notice_fields_for_db(notice)
        _apply_notice_fields(existing, fields)
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
        existing = _find_existing_scraper_notice(db, notice, dedup_key)
        if existing is None:
            fields = _notice_fields_for_db(notice)
            notice_row = ScraperNoticeModel(
                dedup_key=dedup_key,
                first_seen_at=executed_at,
                last_seen_at=executed_at,
                last_run_id=payload.run_id,
            )
            _apply_notice_fields(notice_row, fields)
            db.add(notice_row)
            db.flush()
        else:
            fields = _notice_fields_for_db(notice)
            _apply_notice_fields(existing, fields)
            existing.last_seen_at = executed_at
            existing.last_run_id = payload.run_id

    db.commit()
    return ScraperRunReportResponse(
        success=True,
        message="스크래퍼 실행 결과가 저장되었습니다.",
        run_id=payload.run_id,
    )
