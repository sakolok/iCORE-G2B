import hashlib
import io
import json
import re
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from html import unescape
from urllib.parse import urlparse
from xml.etree import ElementTree

import requests
from sqlalchemy import and_, distinct, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.data.models import ScraperNoticeModel
from app.g2b.bid_notice import (
    REGION_API_EMPTY,
    REGION_API_ERROR,
    REGION_API_ORDER_MISMATCH,
    REGION_API_VALUE,
)
from app.g2b.bid_notices.collector import (
    INDUSTRY_API_EMPTY,
    INDUSTRY_API_ERROR,
    INDUSTRY_API_NONE,
    INDUSTRY_API_ORDER_MISMATCH,
    INDUSTRY_API_VALUE,
    determine_icore_industry_code_match,
    fetch_explicit_region_restriction,
    fetch_industry_restriction_codes,
    fetch_notice_detail_source,
    fetch_participant_region_restriction,
)
from app.g2b.bid_notices.models import (
    BidNoticeDocumentAnalysisModel,
    UserBidNoticeMatchModel,
)

try:  # pragma: no cover - exercised in the deploy image
    from pypdf import PdfReader
except Exception:  # pragma: no cover
    PdfReader = None


ANALYZER_VERSION = "document-rules-v1"
MAX_ATTACHMENT_BYTES = 12 * 1024 * 1024
MAX_EXTRACTED_TEXT_LENGTH = 250_000
RECENT_NOTICE_DAYS = 14
DEFAULT_BATCH_SIZE = 10
MAX_BATCH_SIZE = 20
MAX_ANALYSIS_ATTEMPTS = 3
RETRY_DELAYS = (timedelta(minutes=5), timedelta(minutes=20), timedelta(hours=1))
STALE_CLAIM_AFTER = timedelta(minutes=35)
PRIMARY_NOTICE_DOCUMENT_TOKENS = ("입찰공고서", "입찰공고안", "입찰공고", "공고문")
NON_PRIMARY_DOCUMENT_TOKENS = (
    "제안요청",
    "규격",
    "사양",
    "입찰참가",
    "서식",
    "양식",
    "과업지시",
    "계약조건",
    "질의",
    "답변",
)
REGION_NAMES = (
    "서울특별시",
    "부산광역시",
    "대구광역시",
    "인천광역시",
    "광주광역시",
    "대전광역시",
    "울산광역시",
    "세종특별자치시",
    "경기도",
    "강원특별자치도",
    "강원도",
    "충청북도",
    "충청남도",
    "전북특별자치도",
    "전라북도",
    "전라남도",
    "경상북도",
    "경상남도",
    "제주특별자치도",
    "제주도",
)
REGION_CONTEXT_TERMS = ("참가가능지역", "지역제한", "주된 영업소", "소재지")
INDUSTRY_CONTEXT_TERMS = ("업종", "사업자등록", "면허", "업종코드", "등록코드")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _is_g2b_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and (parsed.hostname or "").lower().endswith(
        "g2b.go.kr"
    )


def _attachment_manifest(source: dict) -> list[tuple[str, str]]:
    attachments: list[tuple[str, str]] = []
    seen: set[str] = set()
    for index in range(1, 11):
        url = str(source.get(f"ntceSpecDocUrl{index}") or "").strip()
        if not url or url in seen or not _is_g2b_url(url):
            continue
        seen.add(url)
        name = str(source.get(f"ntceSpecFileNm{index}") or f"첨부파일 {index}").strip()
        attachments.append((name, url))
    return attachments


def _source_payload(notice: ScraperNoticeModel) -> dict:
    try:
        source = json.loads(notice.source_payload or "{}")
    except (TypeError, ValueError):
        source = {}
    return source if isinstance(source, dict) else {}


def _ensure_notice_attachments(notice: ScraperNoticeModel) -> list[tuple[str, str]]:
    source = _source_payload(notice)
    attachments = _attachment_manifest(source)
    if attachments or source.get("_document_attachments_checked"):
        return attachments

    detail_source = fetch_notice_detail_source(
        notice_no=notice.bid_notice_no or notice.notice_id,
        notice_ord=notice.bid_notice_ord or "00",
        work_type=notice.work_type,
    )
    source["_document_attachments_checked"] = True
    if isinstance(detail_source, dict):
        source.update(detail_source)
    notice.source_payload = json.dumps(source, ensure_ascii=False, sort_keys=True, default=str)
    return _attachment_manifest(source)


def _needs_region_api_refresh(status: str | None) -> bool:
    return status in {None, REGION_API_EMPTY, REGION_API_ERROR, REGION_API_ORDER_MISMATCH}


def _needs_industry_api_refresh(status: str | None) -> bool:
    return status in {
        None,
        INDUSTRY_API_EMPTY,
        INDUSTRY_API_ERROR,
        INDUSTRY_API_ORDER_MISMATCH,
    }


def _needs_region_document(status: str | None) -> bool:
    return status == REGION_API_EMPTY


def _needs_industry_document(status: str | None) -> bool:
    return status == INDUSTRY_API_EMPTY


def _refresh_api_context(notice: ScraperNoticeModel) -> tuple[bool, bool]:
    if _needs_region_api_refresh(notice.region_restriction_api_status):
        region, status = fetch_participant_region_restriction(
            notice_no=notice.bid_notice_no or notice.notice_id,
            notice_ord=notice.bid_notice_ord or "00",
        )
        notice.region_restriction = region
        notice.region_restriction_api_status = status
        if status == REGION_API_VALUE:
            notice.region_restriction_source = "API"
            notice.region_restriction_evidence = None
        elif status == REGION_API_EMPTY:
            region, evidence = fetch_explicit_region_restriction(
                notice_no=notice.bid_notice_no or notice.notice_id,
                notice_ord=notice.bid_notice_ord or "00",
                work_type=notice.work_type,
            )
            if region is not None:
                notice.region_restriction = region
                notice.region_restriction_api_status = REGION_API_VALUE
                notice.region_restriction_source = "API"
                notice.region_restriction_evidence = evidence

    if _needs_industry_api_refresh(notice.industry_restriction_api_status):
        codes, status = fetch_industry_restriction_codes(
            notice_no=notice.bid_notice_no or notice.notice_id,
            notice_ord=notice.bid_notice_ord or "00",
        )
        notice.industry_restriction_codes = codes
        notice.industry_restriction_api_status = status
        if status in {INDUSTRY_API_VALUE, INDUSTRY_API_NONE}:
            notice.industry_restriction_source = "API"
            notice.industry_restriction_evidence = None
    notice.icore_industry_code_match = determine_icore_industry_code_match(
        notice.industry_restriction_api_status,
        notice.industry_restriction_codes,
    )

    return (
        _needs_region_document(notice.region_restriction_api_status),
        _needs_industry_document(notice.industry_restriction_api_status),
    )


def _download_attachment(url: str) -> tuple[bytes, str]:
    if not _is_g2b_url(url):
        raise ValueError("나라장터 첨부파일 주소만 분석할 수 있습니다.")
    response = requests.get(url, timeout=25, stream=True, headers={"Accept": "*/*"})
    response.raise_for_status()
    if not _is_g2b_url(response.url):
        raise ValueError("첨부파일 이동 주소가 나라장터 주소가 아닙니다.")
    content = bytearray()
    for chunk in response.iter_content(chunk_size=64 * 1024):
        content.extend(chunk)
        if len(content) > MAX_ATTACHMENT_BYTES:
            raise ValueError("첨부파일이 분석 허용 크기를 초과했습니다.")
    return bytes(content), (response.headers.get("Content-Type") or "").lower()


def _extract_hwpx_text(content: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        pieces: list[str] = []
        for name in sorted(archive.namelist()):
            if not name.startswith("Contents/") or not name.endswith(".xml"):
                continue
            root = ElementTree.fromstring(archive.read(name))
            pieces.extend(text.strip() for text in root.itertext() if text and text.strip())
    return "\n".join(pieces)


def _extract_text(name: str, content: bytes, content_type: str) -> str:
    suffix = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if suffix == "pdf" or "pdf" in content_type:
        if PdfReader is None:
            raise ValueError("PDF 텍스트 추출기가 준비되지 않았습니다.")
        reader = PdfReader(io.BytesIO(content))
        return "\n".join((page.extract_text() or "") for page in reader.pages)
    if suffix == "hwpx" or content.startswith(b"PK"):
        return _extract_hwpx_text(content)
    if suffix in {"txt", "html", "htm"} or content_type.startswith("text/"):
        return unescape(content.decode("utf-8", errors="ignore"))
    if suffix == "hwp":
        raise ValueError("구형 HWP 문서는 자동 분석 대상이 아닙니다.")
    raise ValueError("지원하지 않는 첨부파일 형식입니다.")


def _context_lines(text: str, terms: tuple[str, ...]) -> list[str]:
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    contexts: list[str] = []
    for index, line in enumerate(lines):
        if any(term in line for term in terms):
            context = " ".join(lines[max(0, index - 1) : min(len(lines), index + 3)]).strip()
            if context and context not in contexts:
                contexts.append(context)
    return contexts


def _first_evidence(contexts: list[str]) -> str | None:
    for context in contexts:
        if context:
            return context[:1000]
    return None


def _analyze_text(text: str) -> dict[str, str | None]:
    text = text[:MAX_EXTRACTED_TEXT_LENGTH]
    region_contexts = _context_lines(text, REGION_CONTEXT_TERMS)
    regions: list[str] = []
    for context in region_contexts:
        for region in REGION_NAMES:
            if region in context and region not in regions:
                regions.append(region)

    industry_contexts = _context_lines(text, INDUSTRY_CONTEXT_TERMS)
    codes: list[str] = []
    for context in industry_contexts:
        for code in re.findall(r"(?<!\d)(\d{4})(?!\d)", context):
            if code not in codes:
                codes.append(code)

    region_explicitly_none = any(
        phrase in context
        for context in region_contexts
        for phrase in ("지역제한 없음", "지역 제한 없음", "지역제한 해당없음")
    )
    industry_explicitly_none = any(
        phrase in context
        for context in industry_contexts
        for phrase in ("업종제한 없음", "업종 제한 없음", "업종코드 해당없음")
    )

    return {
        "region_result": ", ".join(regions) if regions else None,
        "region_status": (
            "DOCUMENT_VALUE"
            if regions
            else "DOCUMENT_NONE"
            if region_explicitly_none
            else None
        ),
        "region_evidence": _first_evidence(region_contexts),
        "industry_codes": ", ".join(codes) if codes else None,
        "industry_status": (
            "DOCUMENT_VALUE"
            if codes
            else "DOCUMENT_NONE"
            if industry_explicitly_none
            else None
        ),
        "industry_evidence": _first_evidence(industry_contexts),
    }


def _analysis_row(
    db: Session,
    *,
    notice_id: int,
    attachment_name: str,
    attachment_url: str,
    needs_region: bool,
    needs_industry: bool,
) -> BidNoticeDocumentAnalysisModel:
    attachment_key = hashlib.sha256(attachment_url.encode("utf-8")).hexdigest()
    row = db.execute(
        select(BidNoticeDocumentAnalysisModel).where(
            BidNoticeDocumentAnalysisModel.notice_id == notice_id,
            BidNoticeDocumentAnalysisModel.attachment_key == attachment_key,
            BidNoticeDocumentAnalysisModel.analyzer_version == ANALYZER_VERSION,
        )
    ).scalar_one_or_none()
    if row is None:
        candidate = BidNoticeDocumentAnalysisModel(
            notice_id=notice_id,
            attachment_key=attachment_key,
            attachment_name=attachment_name[:500],
            attachment_url=attachment_url,
            analyzer_version=ANALYZER_VERSION,
            is_primary_notice_document=True,
            needs_region=needs_region,
            needs_industry=needs_industry,
        )
        try:
            with db.begin_nested():
                db.add(candidate)
                db.flush()
            row = candidate
        except IntegrityError:
            row = db.execute(
                select(BidNoticeDocumentAnalysisModel).where(
                    BidNoticeDocumentAnalysisModel.notice_id == notice_id,
                    BidNoticeDocumentAnalysisModel.attachment_key == attachment_key,
                    BidNoticeDocumentAnalysisModel.analyzer_version == ANALYZER_VERSION,
                )
            ).scalar_one()
    else:
        row.is_primary_notice_document = True
    row.needs_region = row.needs_region or needs_region
    row.needs_industry = row.needs_industry or needs_industry
    return row


def _primary_notice_attachment(
    attachments: list[tuple[str, str]],
) -> tuple[str, str] | None:
    candidates: list[tuple[int, int, str, str]] = []
    for position, (name, url) in enumerate(attachments):
        normalized_name = re.sub(r"[\s_\-·()\[\]]+", "", name).casefold()
        if any(token in normalized_name for token in NON_PRIMARY_DOCUMENT_TOKENS):
            continue
        for rank, token in enumerate(PRIMARY_NOTICE_DOCUMENT_TOKENS):
            if token in normalized_name:
                candidates.append((rank, position, name, url))
                break
    if not candidates:
        return None
    _, _, name, url = min(candidates, key=lambda item: (item[0], item[1]))
    return name, url


def _mark_missing_primary_document(
    db: Session,
    *,
    notice_id: int,
    needs_region: bool,
    needs_industry: bool,
) -> bool:
    attachment_name = "입찰공고 문서 없음"
    attachment_url = ""
    row = _analysis_row(
        db,
        notice_id=notice_id,
        attachment_name=attachment_name,
        attachment_url=attachment_url,
        needs_region=needs_region,
        needs_industry=needs_industry,
    )
    if row.status in {"SUCCEEDED", "REVIEW_REQUIRED", "UNSUPPORTED"}:
        return False
    row.status = "REVIEW_REQUIRED"
    row.error_message = "분석 가능한 입찰공고 또는 공고문 첨부파일이 없습니다."
    row.analyzed_at = _utcnow()
    return True


def _apply_document_results(db: Session, notice: ScraperNoticeModel) -> None:
    rows = db.execute(
        select(BidNoticeDocumentAnalysisModel).where(
            BidNoticeDocumentAnalysisModel.notice_id == notice.id,
            BidNoticeDocumentAnalysisModel.status == "SUCCEEDED",
            BidNoticeDocumentAnalysisModel.is_primary_notice_document.is_(True),
        )
    ).scalars().all()
    regions: list[str] = []
    codes: list[str] = []
    region_evidence = None
    industry_evidence = None
    region_explicitly_none = False
    industry_explicitly_none = False
    for row in rows:
        if row.region_result:
            for region in row.region_result.split(", "):
                if region and region not in regions:
                    regions.append(region)
            region_evidence = region_evidence or row.evidence
        if row.industry_codes:
            for code in row.industry_codes.split(", "):
                if code and code not in codes:
                    codes.append(code)
            industry_evidence = industry_evidence or row.evidence
        region_explicitly_none = region_explicitly_none or row.region_status == "DOCUMENT_NONE"
        industry_explicitly_none = industry_explicitly_none or row.industry_status == "DOCUMENT_NONE"

    if regions and _needs_region_document(notice.region_restriction_api_status):
        notice.region_restriction = ", ".join(regions)
        notice.region_restriction_api_status = "DOCUMENT_VALUE"
        notice.region_restriction_source = "DOCUMENT"
        notice.region_restriction_evidence = region_evidence
    elif region_explicitly_none and _needs_region_document(notice.region_restriction_api_status):
        notice.region_restriction = "해당없음"
        notice.region_restriction_api_status = "DOCUMENT_NONE"
        notice.region_restriction_source = "DOCUMENT"
        notice.region_restriction_evidence = region_evidence
    if codes and _needs_industry_document(notice.industry_restriction_api_status):
        notice.industry_restriction_codes = ", ".join(codes)
        notice.industry_restriction_api_status = "DOCUMENT_VALUE"
        notice.industry_restriction_source = "DOCUMENT"
        notice.industry_restriction_evidence = industry_evidence
    elif industry_explicitly_none and _needs_industry_document(notice.industry_restriction_api_status):
        notice.industry_restriction_codes = None
        notice.industry_restriction_api_status = "DOCUMENT_NONE"
        notice.industry_restriction_source = "DOCUMENT"
        notice.industry_restriction_evidence = industry_evidence
    notice.icore_industry_code_match = determine_icore_industry_code_match(
        notice.industry_restriction_api_status,
        notice.industry_restriction_codes,
    )


def _queue_candidates(
    db: Session,
    *,
    current: datetime,
) -> tuple[int, int, int]:
    cutoff = current - timedelta(days=RECENT_NOTICE_DAYS)
    candidate_ids = db.execute(
        select(distinct(UserBidNoticeMatchModel.notice_id))
        .join(ScraperNoticeModel, ScraperNoticeModel.id == UserBidNoticeMatchModel.notice_id)
        .where(
            UserBidNoticeMatchModel.is_current_match.is_(True),
            ScraperNoticeModel.published_at >= cutoff,
        )
    ).scalars().all()
    queued = review_required = 0
    for notice_id in candidate_ids:
        notice = db.get(ScraperNoticeModel, notice_id)
        if notice is None:
            continue
        needs_region, needs_industry = _refresh_api_context(notice)
        if not needs_region and not needs_industry:
            continue
        attachment = _primary_notice_attachment(_ensure_notice_attachments(notice))
        if attachment is None:
            review_required += int(
                _mark_missing_primary_document(
                    db,
                    notice_id=notice.id,
                    needs_region=needs_region,
                    needs_industry=needs_industry,
                )
            )
            continue
        attachment_name, attachment_url = attachment
        row = _analysis_row(
            db,
            notice_id=notice.id,
            attachment_name=attachment_name,
            attachment_url=attachment_url,
            needs_region=needs_region,
            needs_industry=needs_industry,
        )
        if row.status not in {"SUCCEEDED", "REVIEW_REQUIRED", "UNSUPPORTED", "RUNNING"}:
            queued += 1
    return len(candidate_ids), queued, review_required


def _claimable_analysis_condition(current: datetime):
    stale_before = current - STALE_CLAIM_AFTER
    return or_(
        BidNoticeDocumentAnalysisModel.status == "PENDING",
        and_(
            BidNoticeDocumentAnalysisModel.status == "FAILED",
            BidNoticeDocumentAnalysisModel.attempt_count < MAX_ANALYSIS_ATTEMPTS,
            or_(
                BidNoticeDocumentAnalysisModel.next_retry_at.is_(None),
                BidNoticeDocumentAnalysisModel.next_retry_at <= current,
            ),
        ),
        and_(
            BidNoticeDocumentAnalysisModel.status == "RUNNING",
            BidNoticeDocumentAnalysisModel.claimed_at.is_not(None),
            BidNoticeDocumentAnalysisModel.claimed_at < stale_before,
        ),
    )


def _claim_analysis_batch(
    db: Session,
    *,
    current: datetime,
    batch_size: int,
) -> tuple[str, list[BidNoticeDocumentAnalysisModel]]:
    claim_token = str(uuid.uuid4())
    condition = _claimable_analysis_condition(current)
    candidate_ids = db.scalars(
        select(BidNoticeDocumentAnalysisModel.id)
        .where(
            BidNoticeDocumentAnalysisModel.is_primary_notice_document.is_(True),
            condition,
        )
        .order_by(BidNoticeDocumentAnalysisModel.created_at, BidNoticeDocumentAnalysisModel.id)
        .limit(batch_size * 3)
    ).all()
    claimed_ids: list[int] = []
    for analysis_id in candidate_ids:
        if len(claimed_ids) == batch_size:
            break
        result = db.execute(
            update(BidNoticeDocumentAnalysisModel)
            .where(
                BidNoticeDocumentAnalysisModel.id == analysis_id,
                BidNoticeDocumentAnalysisModel.is_primary_notice_document.is_(True),
                _claimable_analysis_condition(current),
            )
            .values(
                status="RUNNING",
                claim_token=claim_token,
                claimed_at=current,
                next_retry_at=None,
            )
        )
        if result.rowcount:
            claimed_ids.append(analysis_id)
    db.commit()
    if not claimed_ids:
        return claim_token, []
    rows = db.scalars(
        select(BidNoticeDocumentAnalysisModel)
        .where(
            BidNoticeDocumentAnalysisModel.id.in_(claimed_ids),
            BidNoticeDocumentAnalysisModel.claim_token == claim_token,
        )
        .order_by(BidNoticeDocumentAnalysisModel.id)
    ).all()
    return claim_token, rows


def _retry_at(current: datetime, attempt_count: int) -> datetime | None:
    if attempt_count >= MAX_ANALYSIS_ATTEMPTS:
        return None
    return current + RETRY_DELAYS[min(attempt_count - 1, len(RETRY_DELAYS) - 1)]


def _process_claimed_analysis(
    db: Session,
    *,
    row: BidNoticeDocumentAnalysisModel,
    claim_token: str,
    current: datetime,
) -> tuple[int, int, int]:
    notice = db.get(ScraperNoticeModel, row.notice_id)
    if notice is None or row.claim_token != claim_token or row.status != "RUNNING":
        return 0, 0, 0
    row.attempt_count += 1
    try:
        content, content_type = _download_attachment(row.attachment_url)
        row.content_sha256 = hashlib.sha256(content).hexdigest()
        findings = _analyze_text(_extract_text(row.attachment_name, content, content_type))
        row.region_result = findings["region_result"]
        row.region_status = findings["region_status"]
        row.industry_codes = findings["industry_codes"]
        row.industry_status = findings["industry_status"]
        evidence = findings["region_evidence"] or findings["industry_evidence"]
        row.evidence = f"{row.attachment_name}: {evidence}"[:1200] if evidence else row.attachment_name
        row.status = (
            "SUCCEEDED"
            if (
                row.region_result
                or row.industry_codes
                or row.region_status == "DOCUMENT_NONE"
                or row.industry_status == "DOCUMENT_NONE"
            )
            else "REVIEW_REQUIRED"
        )
        row.error_message = None
        row.analyzed_at = current
        row.claim_token = None
        row.claimed_at = None
        _apply_document_results(db, notice)
        db.commit()
        return int(row.status == "SUCCEEDED"), int(row.status == "REVIEW_REQUIRED"), 0
    except ValueError as error:
        message = str(error)
        row.status = (
            "UNSUPPORTED"
            if "지원하지 않는" in message or "구형 HWP" in message
            else "REVIEW_REQUIRED"
        )
        row.error_message = message[:1200]
        row.analyzed_at = current
        row.claim_token = None
        row.claimed_at = None
        db.commit()
        return 0, 1, 0
    except requests.RequestException as error:
        row.status = "FAILED"
        row.error_message = str(error)[:1200]
        row.next_retry_at = _retry_at(current, row.attempt_count)
        row.claim_token = None
        row.claimed_at = None
        db.commit()
        return 0, 0, 1
    except Exception as error:
        row.status = "FAILED"
        row.error_message = str(error)[:1200]
        row.next_retry_at = _retry_at(current, row.attempt_count)
        row.claim_token = None
        row.claimed_at = None
        db.commit()
        return 0, 0, 1


def run_pending_bid_notice_document_analysis(
    db: Session,
    *,
    now: datetime | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    prepare_queue: bool = True,
) -> dict[str, int]:
    current = now or _utcnow()
    bounded_batch_size = max(1, min(batch_size, MAX_BATCH_SIZE))
    candidate_count = queued = review_required = 0
    if prepare_queue:
        candidate_count, queued, review_required = _queue_candidates(db, current=current)
        db.commit()
    claim_token, claimed_rows = _claim_analysis_batch(
        db,
        current=current,
        batch_size=bounded_batch_size,
    )
    analyzed = failed = 0
    for row in claimed_rows:
        processed, reviewed, failed_count = _process_claimed_analysis(
            db,
            row=row,
            claim_token=claim_token,
            current=current,
        )
        analyzed += processed
        review_required += reviewed
        failed += failed_count

    return {
        "candidate_count": candidate_count,
        "queued_count": queued,
        "claimed_count": len(claimed_rows),
        "analyzed_count": analyzed,
        "review_required_count": review_required,
        "failed_count": failed,
    }
