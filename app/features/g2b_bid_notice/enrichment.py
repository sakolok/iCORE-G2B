"""Attachment-backed enrichment for selected G2B bid notices.

The normal search remains a fast public-API list query.  This module is called
only for notices selected by the user, so it stays below the public API's daily
traffic limit while providing evidence before a Sheet save.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import zlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urljoin, urlparse
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile
from zoneinfo import ZoneInfo

import requests

from app.features.g2b_bid_notice.schemas import (
    BidNoticePreviewItem,
    EnrichmentCheck,
    LocalAttachmentFile,
    NoticeAttachmentSource,
    PersonalCollectionSettings,
)
from app.features.g2b_bid_notice.contracts import bid_notice_dedup_key


G2B_API_BASE_URL = "https://apis.data.go.kr/1230000/ad/BidPublicInfoService"
KST = ZoneInfo("Asia/Seoul")
ATTACHMENT_ROOT = Path(__file__).resolve().parents[3] / ".local" / "g2b-attachments"
API_PAGE_SIZE = 100
MAX_ENRICHMENT_PAGES = 3
MAX_SUPPLEMENT_PAGES = 50
MAX_ATTACHMENT_BYTES = 35 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 100
MAX_ARCHIVE_UNCOMPRESSED_BYTES = 80 * 1024 * 1024
ALLOWED_INDUSTRY_CODES = {
    "9901",
    "3198",
    "0036",
    "1169",
    "1261",
    "1426",
    "1468",
    "9999",
}

# A regional participation condition is often written as an applicant's
# registered address rather than as the literal term "지역제한".  Keep the
# canonical display value separate from common shortened forms found in
# attachment text.
REGION_NAME_ALIASES = {
    "서울특별시": "서울특별시",
    "서울시": "서울특별시",
    "부산광역시": "부산광역시",
    "부산시": "부산광역시",
    "대구광역시": "대구광역시",
    "대구시": "대구광역시",
    "인천광역시": "인천광역시",
    "인천시": "인천광역시",
    "광주광역시": "광주광역시",
    "대전광역시": "대전광역시",
    "대전시": "대전광역시",
    "울산광역시": "울산광역시",
    "울산시": "울산광역시",
    "세종특별자치시": "세종특별자치시",
    "세종시": "세종특별자치시",
    "경기도": "경기도",
    "강원특별자치도": "강원특별자치도",
    "강원도": "강원특별자치도",
    "충청북도": "충청북도",
    "충북": "충청북도",
    "충청남도": "충청남도",
    "충남": "충청남도",
    "전북특별자치도": "전북특별자치도",
    "전라북도": "전북특별자치도",
    "전북": "전북특별자치도",
    "전라남도": "전라남도",
    "전남": "전라남도",
    "경상북도": "경상북도",
    "경북": "경상북도",
    "경상남도": "경상남도",
    "경남": "경상남도",
    "제주특별자치도": "제주특별자치도",
    "제주도": "제주특별자치도",
}
REGION_NAME_PATTERN = re.compile(
    "|".join(re.escape(name) for name in sorted(REGION_NAME_ALIASES, key=len, reverse=True))
)
RESIDENCY_FIELD_PATTERN = re.compile(r"소재지|주된\s*영업소|본점|사업장|주소지|등록지")
RESIDENCY_ELIGIBILITY_PATTERN = re.compile(
    r"(?:"
    # "경상북도에 있는 업체", "서울특별시 내에 둔 자" and
    # "전라남도인 업체" are all common ways to express the same bidder
    # location eligibility rule.  The final subject is intentionally limited
    # to a bidder/business term so a programme participant's residence does
    # not become an apparent bid regional restriction.
    r"(?:에|으로?|내에)\s*(?:있(?:는|어야)|소재(?:한|하여)|위치(?:한|하여)|"
    r"두(?:고\s*있(?:는|어야)|어야)|둔|되어\s*있(?:는|어야))\s*(?:업체|사업자|법인|자)"
    r"|(?:도|시|군)인\s*(?:업체|사업자|법인|자)"
    r"|(?:소재지|주된\s*영업소|본점|사업장|주소지|등록지)[\s\S]{0,100}?"
    r"(?:이어야|여야|일\s*것|로\s*제한|으로\s*제한|인\s*(?:업체|사업자|법인|자))"
    r")"
)


@dataclass
class ExtractedAttachment:
    attachment: LocalAttachmentFile | None
    text: str
    source_name: str | None = None

    @property
    def display_name(self) -> str:
        if self.source_name:
            return self.source_name
        if self.attachment:
            return self.attachment.file_name
        return "나라장터 공고 상세 페이지"


@dataclass
class SupplementIndex:
    licenses: dict[tuple[str, str], list[dict[str, Any]]]
    regions: dict[tuple[str, str], list[dict[str, Any]]]
    attachments: dict[tuple[str, str], list[NoticeAttachmentSource]]
    errors: dict[str, str]


class G2BEnrichmentError(ValueError):
    """A safe error for a selected-notice enrichment request."""


def _normalize_service_key(raw_key: str) -> str:
    key = raw_key.strip()
    for _ in range(2):
        decoded = unquote(key)
        if decoded == key:
            break
        key = decoded
    return key


def _clean_text(value: object) -> str | None:
    text = str("" if value is None else value).strip()
    return text or None


def _merge_attachment_sources(*source_groups: list[NoticeAttachmentSource]) -> list[NoticeAttachmentSource]:
    """Keep every real download URL once, even when file names repeat."""

    merged: list[NoticeAttachmentSource] = []
    seen_urls: set[str] = set()
    for sources in source_groups:
        for source in sources:
            url = _clean_text(source.download_url)
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            merged.append(source)
    return merged


def attachment_sources_from_notice_item(item: dict[str, Any]) -> list[NoticeAttachmentSource]:
    """Read the actual attachment links supplied with a bid-notice list row.

    The G2B notice-list response exposes the same specification/document links
    that appear under a notice's detail page.  These links are more complete
    for ordinary bid notices than the e-order attachment-list API, which only
    covers a subset of notices.
    """

    sources: list[NoticeAttachmentSource] = []
    for index in range(1, 11):
        url = _clean_text(item.get(f"ntceSpecDocUrl{index}"))
        name = _clean_text(item.get(f"ntceSpecFileNm{index}"))
        if url:
            sources.append(
                NoticeAttachmentSource(
                    file_name=name or f"첨부파일 {index}",
                    download_url=url,
                    source_type="나라장터 공고 상세 첨부파일",
                )
            )

    # Some notices expose a standard document URL outside the numbered slots.
    # Add it only if it is a distinct URL.
    standard_url = _clean_text(item.get("stdNtceDocUrl"))
    if standard_url:
        sources.append(
            NoticeAttachmentSource(
                file_name=_clean_text(item.get("stdNtceDocFileNm") or item.get("stdNtceDocNm"))
                or "표준 공고문",
                download_url=standard_url,
                source_type="나라장터 공고 상세 첨부파일",
            )
        )
    return _merge_attachment_sources(sources)


def _extract_items(payload: object) -> tuple[list[dict[str, Any]], int | None]:
    if not isinstance(payload, dict):
        return [], None
    response = payload.get("response")
    if not isinstance(response, dict):
        return [], None
    header = response.get("header") or {}
    result_code = str(header.get("resultCode") or "") if isinstance(header, dict) else ""
    if result_code and result_code != "00":
        raise G2BEnrichmentError("나라장터 상세 보강 API가 정상 응답하지 않았습니다.")
    body = response.get("body") or {}
    if not isinstance(body, dict):
        return [], None
    total_raw = body.get("totalCount")
    try:
        total_count = int(str(total_raw).replace(",", "")) if total_raw not in (None, "") else None
    except ValueError:
        total_count = None
    items = body.get("items")
    if isinstance(items, dict):
        items = items.get("item")
    if isinstance(items, dict):
        items = [items]
    if isinstance(items, list):
        return [item for item in items if isinstance(item, dict)], total_count
    return [], total_count


def _collection_dates(settings: PersonalCollectionSettings) -> tuple[str, str]:
    now = datetime.now(KST)
    start_date = settings.posted_date_start or (now - timedelta(days=14)).date()
    end_date = min(settings.posted_date_end or now.date(), now.date())
    return f"{start_date:%Y%m%d}0000", f"{end_date:%Y%m%d}2359"


def _fetch_operation_for_collection(
    operation: str, settings: PersonalCollectionSettings, *, max_pages: int | None = None
) -> list[dict[str, Any]]:
    service_key = _normalize_service_key(os.getenv("G2B_SERVICE_KEY", ""))
    if not service_key:
        raise G2BEnrichmentError("나라장터 API 키 설정을 확인할 수 없습니다.")
    start_at, end_at = _collection_dates(settings)
    all_items: list[dict[str, Any]] = []
    reported_total: int | None = None
    page_no = 1
    while True:
        params = {
            "serviceKey": service_key,
            "type": "json",
            "numOfRows": API_PAGE_SIZE,
            "pageNo": page_no,
            "inqryDiv": "1",
            "inqryBgnDt": start_at,
            "inqryEndDt": end_at,
        }
        try:
            response = requests.get(
                f"{G2B_API_BASE_URL}/{operation}",
                params=params,
                headers={"Accept": "application/json"},
                timeout=25,
            )
            response.raise_for_status()
        except requests.RequestException as error:
            raise G2BEnrichmentError("나라장터 보강 목록 API와 통신하지 못했습니다.") from error
        page_items, total_count = _extract_items(response.json())
        all_items.extend(page_items)
        reported_total = total_count if total_count is not None else reported_total
        if not page_items or len(page_items) < API_PAGE_SIZE:
            break
        if reported_total is not None and page_no * API_PAGE_SIZE >= reported_total:
            break
        if max_pages is not None and page_no >= max_pages:
            break
        if page_no >= MAX_SUPPLEMENT_PAGES:
            raise G2BEnrichmentError("보강 목록 결과가 너무 많아 자동 수집을 중단했습니다.")
        page_no += 1
    return all_items


def _group_notice_rows(rows: list[dict[str, Any]]) -> dict[tuple[str, str], list[dict[str, Any]]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        key = bid_notice_dedup_key(_clean_text(row.get("bidNtceNo")), _clean_text(row.get("bidNtceOrd")))
        if key is not None:
            grouped.setdefault(key, []).append(row)
    return grouped


def collect_supplement_index(
    settings: PersonalCollectionSettings,
    *,
    max_pages: int | None = None,
) -> SupplementIndex:
    """Fetch each public supplemental list once for the collection period."""

    results: dict[str, list[dict[str, Any]]] = {}
    errors: dict[str, str] = {}
    operations = {"attachment": "getBidPblancListInfoEorderAtchFileInfo"}
    for name, operation in operations.items():
        try:
            results[name] = _fetch_operation_for_collection(
                operation,
                settings,
                max_pages=max_pages,
            )
        except G2BEnrichmentError as error:
            results[name] = []
            errors[name] = str(error)

    attachment_index: dict[tuple[str, str], list[NoticeAttachmentSource]] = {}
    for key, rows in _group_notice_rows(results["attachment"]).items():
        sources: list[NoticeAttachmentSource] = []
        seen: set[tuple[str, str]] = set()
        for row in rows:
            name = _clean_text(row.get("eorderAtchFileNm") or row.get("atchFileNm"))
            url = _clean_text(row.get("eorderAtchFileUrl") or row.get("atchFileUrl"))
            if not name or not url or (name, url) in seen:
                continue
            seen.add((name, url))
            sources.append(NoticeAttachmentSource(file_name=name, download_url=url, source_type="e발주 첨부파일"))
        attachment_index[key] = sources

    return SupplementIndex(
        licenses={},
        regions={},
        attachments=attachment_index,
        errors=errors,
    )


def preview_enrichment_fields(item: BidNoticePreviewItem, index: SupplementIndex | None = None) -> dict[str, Any]:
    key = bid_notice_dedup_key(item.bid_notice_no, item.bid_notice_ord)
    indexed_sources = index.attachments.get(key, []) if index and key else []
    sources = _merge_attachment_sources(item.attachment_sources, indexed_sources)
    if sources:
        attachment_label = f"상세 첨부파일 {len(sources)}개 확인"
    elif item.source_url:
        attachment_label = "상세 페이지 첨부파일은 선택 분석 시 확인"
    else:
        attachment_label = "상세 페이지 URL 없음"
    return {
        "attachment_sources": sources,
        "attachment_lookup_label": attachment_label,
    }


def _source_query_dates(item: BidNoticePreviewItem) -> tuple[str, str]:
    reference = item.published_at or datetime.now(KST)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=KST)
    else:
        reference = reference.astimezone(KST)
    return (
        (reference - timedelta(minutes=1)).strftime("%Y%m%d%H%M"),
        (reference + timedelta(minutes=1)).strftime("%Y%m%d%H%M"),
    )


def _same_notice(entry: dict[str, Any], item: BidNoticePreviewItem) -> bool:
    """Never attach another notice's file when an API ignores bidNtceNo."""

    if _clean_text(entry.get("bidNtceNo")) != item.bid_notice_no:
        return False
    entry_ord = _clean_text(entry.get("bidNtceOrd"))
    item_ord = _clean_text(item.bid_notice_ord)
    if entry_ord is None or item_ord is None:
        return True
    if entry_ord.isdigit() and item_ord.isdigit():
        return int(entry_ord) == int(item_ord)
    return entry_ord == item_ord


def _fetch_notice_operation(
    operation: str,
    item: BidNoticePreviewItem,
    *,
    allow_unmatched_result: bool = False,
) -> list[dict[str, Any]]:
    service_key = _normalize_service_key(os.getenv("G2B_SERVICE_KEY", ""))
    if not service_key:
        raise G2BEnrichmentError("나라장터 API 키 설정을 확인할 수 없습니다.")
    if not item.bid_notice_no:
        raise G2BEnrichmentError("공고번호가 없는 공고는 상세 보강할 수 없습니다.")

    start_at, end_at = _source_query_dates(item)
    page_no = 1
    all_items: list[dict[str, Any]] = []
    reported_total: int | None = None
    while True:
        params: dict[str, str | int] = {
            "serviceKey": service_key,
            "type": "json",
            "numOfRows": API_PAGE_SIZE,
            "pageNo": page_no,
            "inqryDiv": "1",
            "inqryBgnDt": start_at,
            "inqryEndDt": end_at,
            "bidNtceNo": item.bid_notice_no,
        }
        if item.bid_notice_ord:
            params["bidNtceOrd"] = item.bid_notice_ord
        try:
            response = requests.get(
                f"{G2B_API_BASE_URL}/{operation}",
                params=params,
                headers={"Accept": "application/json"},
                timeout=25,
            )
            response.raise_for_status()
        except requests.HTTPError as error:
            status = error.response.status_code if error.response is not None else "unknown"
            raise G2BEnrichmentError(f"나라장터 상세 보강 API 호출에 실패했습니다 (HTTP {status}).") from error
        except requests.RequestException as error:
            raise G2BEnrichmentError("나라장터 상세 보강 API와 통신하지 못했습니다.") from error

        page_items, total_count = _extract_items(response.json())
        all_items.extend(page_items)
        if total_count is not None:
            reported_total = total_count
        if not page_items:
            break
        if reported_total is not None and page_no * API_PAGE_SIZE >= reported_total:
            break
        if len(page_items) < API_PAGE_SIZE:
            break
        if page_no >= MAX_ENRICHMENT_PAGES:
            raise G2BEnrichmentError("선택 공고의 상세 API 결과가 너무 많아 자동 판정을 중단했습니다.")
        page_no += 1
    matched = [entry for entry in all_items if _same_notice(entry, item)]
    if all_items and not matched:
        if allow_unmatched_result:
            return []
        raise G2BEnrichmentError("선택 공고와 일치하는 상세 API 원본을 찾지 못했습니다.")
    return matched


def _try_fetch(
    operation: str,
    item: BidNoticePreviewItem,
    *,
    allow_unmatched_result: bool = False,
) -> tuple[list[dict[str, Any]], str | None]:
    try:
        return _fetch_notice_operation(
            operation,
            item,
            allow_unmatched_result=allow_unmatched_result,
        ), None
    except G2BEnrichmentError as error:
        return [], str(error)


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _region_names_in_text(text: str) -> list[str]:
    """Return canonical province/metropolitan-city names in source order."""

    return _unique(
        [REGION_NAME_ALIASES[match.group(0)] for match in REGION_NAME_PATTERN.finditer(text)]
    )


def _industry_restriction(items: list[dict[str, Any]], error: str | None) -> EnrichmentCheck:
    if error:
        return EnrichmentCheck(state="REVIEW", label="확인 필요 (업종제한 API 조회 실패)", evidence=[error])
    if not items:
        return EnrichmentCheck(state="NO_RESTRICTION", label="제한 없음", evidence=["나라장터 업종제한 API: 제한 항목 없음"])

    source_values = _unique(
        [
            _clean_text(entry.get(field_name)) or ""
            for entry in items
            for field_name in ("lcnsLmtNm", "permsnIndstrytyList", "indstrytyMfrcFldList")
        ]
    )
    codes = _unique(re.findall(r"(?<!\d)(\d{4})(?!\d)", " ".join(source_values)))
    allowed_codes = [code for code in codes if code in ALLOWED_INDUSTRY_CODES]
    evidence = source_values[:5] or ["나라장터 업종제한 API: 제한 항목 반환"]
    if allowed_codes:
        return EnrichmentCheck(
            state="PASS",
            label=f"통과: {', '.join(allowed_codes)}",
            evidence=evidence,
        )
    if codes:
        return EnrichmentCheck(
            state="FAIL",
            label=f"불일치: {', '.join(codes)}",
            evidence=evidence,
        )
    return EnrichmentCheck(
        state="REVIEW",
        label="확인 필요 (제한 항목의 4자리 기관코드 미확인)",
        evidence=evidence,
    )


def _region_restriction(items: list[dict[str, Any]], error: str | None) -> EnrichmentCheck:
    if error:
        return EnrichmentCheck(state="REVIEW", label="확인 필요 (지역제한 API 조회 실패)", evidence=[error])
    regions = _unique([_clean_text(entry.get("prtcptPsblRgnNm")) or "" for entry in items])
    if not regions:
        return EnrichmentCheck(state="NO_RESTRICTION", label="제한 없음", evidence=["나라장터 참가가능지역 API: 제한 지역 없음"])
    return EnrichmentCheck(
        state="PASS",
        label=f"제한: {', '.join(regions)}",
        evidence=regions,
    )


def _safe_path_part(value: str | None, fallback: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", value or "").strip(" ._")
    return cleaned[:120] or fallback


def _attachment_directory(item: BidNoticePreviewItem) -> Path:
    return ATTACHMENT_ROOT / _safe_path_part(item.bid_notice_no, "unknown-notice") / _safe_path_part(item.bid_notice_ord, "no-order")


def _attachment_filename(entry: dict[str, Any]) -> str:
    raw_name = _clean_text(entry.get("eorderAtchFileNm") or entry.get("atchFileNm"))
    if raw_name:
        return _safe_path_part(Path(raw_name).name, "attachment")
    raw_url = _clean_text(entry.get("eorderAtchFileUrl") or entry.get("atchFileUrl")) or ""
    parsed_name = Path(urlparse(raw_url).path).name
    return _safe_path_part(parsed_name, "attachment")


def _download_attachment(item: BidNoticePreviewItem, source: NoticeAttachmentSource) -> LocalAttachmentFile:
    file_name = _safe_path_part(Path(source.file_name).name, "attachment")
    raw_url = source.download_url
    # Multiple G2B files can have the same visible name.  Make only the local
    # storage name unique so every attachment is retained while the UI keeps
    # showing the original filename.
    path = Path(file_name)
    url_hash = hashlib.sha1(raw_url.encode("utf-8")).hexdigest()[:10] if raw_url else "missing-url"
    storage_name = f"{path.stem}-{url_hash}{path.suffix}"
    target = _attachment_directory(item) / storage_name
    if target.is_file() and target.stat().st_size > 0:
        return LocalAttachmentFile(
            file_name=file_name,
            local_path=str(target),
            source_type=source.source_type,
            extraction_status="TEXT_EXTRACTED",
            extraction_message="기존 로컬 파일 사용",
        )
    if not raw_url or urlparse(raw_url).scheme not in {"http", "https"}:
        return LocalAttachmentFile(
            file_name=file_name,
            local_path=str(target),
            source_type=source.source_type,
            extraction_status="DOWNLOAD_FAILED",
            extraction_message="다운로드 URL을 확인할 수 없음",
        )

    try:
        response = requests.get(raw_url, stream=True, timeout=45)
        response.raise_for_status()
        content_length = response.headers.get("Content-Length")
        if content_length and int(content_length) > MAX_ATTACHMENT_BYTES:
            raise ValueError("첨부파일 크기 제한 초과")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.part")
        written = 0
        with temporary.open("wb") as destination:
            for chunk in response.iter_content(chunk_size=1024 * 256):
                if not chunk:
                    continue
                written += len(chunk)
                if written > MAX_ATTACHMENT_BYTES:
                    raise ValueError("첨부파일 크기 제한 초과")
                destination.write(chunk)
        temporary.replace(target)
        return LocalAttachmentFile(
            file_name=file_name,
            local_path=str(target),
            source_type=source.source_type,
            extraction_status="TEXT_EXTRACTED",
            extraction_message="다운로드 완료",
        )
    except (requests.RequestException, OSError, ValueError):
        return LocalAttachmentFile(
            file_name=file_name,
            local_path=str(target),
            source_type=source.source_type,
            extraction_status="DOWNLOAD_FAILED",
            extraction_message="첨부파일 다운로드 실패 또는 파일 크기 제한 초과",
        )


def _cached_attachments(item: BidNoticePreviewItem) -> list[LocalAttachmentFile]:
    """Reuse already-downloaded notice files when G2B link discovery fails.

    A re-query can receive a list row without attachment URLs even though the
    same notice's files were successfully downloaded during an earlier query.
    Those local files are still valid evidence and must be analysed again.
    """

    directory = _attachment_directory(item)
    if not directory.is_dir():
        return []

    attachments: list[LocalAttachmentFile] = []
    for path in sorted(directory.iterdir()):
        if not path.is_file() or path.name.endswith(".part") or path.stat().st_size <= 0:
            continue
        display_stem = re.sub(r"-[0-9a-f]{10}$", "", path.stem)
        attachments.append(
            LocalAttachmentFile(
                file_name=f"{display_stem}{path.suffix}",
                local_path=str(path),
                source_type="로컬 캐시",
                extraction_status="TEXT_EXTRACTED",
                extraction_message="기존 로컬 첨부파일 재분석",
            )
        )
    return attachments


def _normalise_document_text(text: str) -> str:
    return re.sub(r"[\t\r\f\v ]+", " ", text).replace("\x00", " ").strip()


def _extract_zip_xml(path: Path, names: list[str]) -> str:
    fragments: list[str] = []
    with ZipFile(path) as archive:
        for name in names:
            if name not in archive.namelist():
                continue
            root = ElementTree.fromstring(archive.read(name))
            fragments.append(" ".join(root.itertext()))
    return _normalise_document_text("\n".join(fragments))


def _extract_docx_text(path: Path) -> str:
    with ZipFile(path) as archive:
        names = [
            name
            for name in archive.namelist()
            if name == "word/document.xml" or re.fullmatch(r"word/(header|footer)\d+\.xml", name)
        ]
    return _extract_zip_xml(path, names)


def _extract_hwpx_text(path: Path) -> str:
    with ZipFile(path) as archive:
        names = [name for name in archive.namelist() if re.fullmatch(r"Contents/section\d+\.xml", name)]
    return _extract_zip_xml(path, names)


def _extract_xlsx_text(path: Path) -> str:
    with ZipFile(path) as archive:
        names = [
            name
            for name in archive.namelist()
            if name == "xl/sharedStrings.xml" or re.fullmatch(r"xl/worksheets/sheet\d+\.xml", name)
        ]
    return _extract_zip_xml(path, names)


def _extract_xls_text(path: Path) -> str:
    """Extract cells from the legacy binary Excel format used in older notices."""

    import xlrd

    workbook = xlrd.open_workbook(str(path), on_demand=True)
    fragments: list[str] = []
    for sheet_index in range(workbook.nsheets):
        sheet = workbook.sheet_by_index(sheet_index)
        for row_index in range(sheet.nrows):
            values = [str(value).strip() for value in sheet.row_values(row_index) if str(value).strip()]
            if values:
                fragments.append(" ".join(values))
    return _normalise_document_text("\n".join(fragments))


def _extract_archive_text(path: Path) -> str:
    """Inspect every supported document inside a ZIP attachment without unpacking it permanently."""

    with ZipFile(path) as archive:
        entries = [entry for entry in archive.infolist() if not entry.is_dir()]
        if len(entries) > MAX_ARCHIVE_ENTRIES:
            raise ValueError("ZIP 첨부파일 항목 수 제한 초과")
        if sum(entry.file_size for entry in entries) > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
            raise ValueError("ZIP 첨부파일 압축 해제 크기 제한 초과")

        fragments: list[str] = []
        with tempfile.TemporaryDirectory(prefix="g2b-archive-") as directory:
            root = Path(directory)
            for index, entry in enumerate(entries, start=1):
                entry_name = _safe_path_part(Path(entry.filename).name, f"archive-file-{index}")
                suffix = Path(entry_name).suffix.lower()
                # Nested archives can expand without a practical bound. Their
                # direct files remain visible to the user, but are not unpacked
                # recursively in one enrichment request.
                if suffix in {".zip", ".7z", ".rar"}:
                    continue
                target = root / f"{index:03d}-{entry_name}"
                target.write_bytes(archive.read(entry))
                child = LocalAttachmentFile(
                    file_name=entry_name,
                    local_path=str(target),
                    source_type="ZIP 첨부파일 내부 문서",
                    extraction_status="TEXT_EXTRACTED",
                    extraction_message="ZIP 내부 문서 추출",
                )
                extracted = _extract_attachment_text(child)
                if extracted.text:
                    fragments.append(f"[{entry.filename}]\n{extracted.text}")
    return _normalise_document_text("\n".join(fragments))


def _extract_hwp_records(raw: bytes) -> str:
    """Read paragraph-text records from an unencrypted HWP 5.x BodyText stream."""

    position = 0
    fragments: list[str] = []
    while position + 4 <= len(raw):
        header = int.from_bytes(raw[position : position + 4], "little")
        tag_id = header & 0x3FF
        size = header >> 20
        position += 4
        if size == 0xFFF:
            if position + 4 > len(raw):
                break
            size = int.from_bytes(raw[position : position + 4], "little")
            position += 4
        if size < 0 or position + size > len(raw):
            break
        data = raw[position : position + size]
        position += size
        if tag_id == 67:
            fragments.append(data.decode("utf-16le", errors="ignore"))
    return _normalise_document_text("\n".join(fragments))


def _extract_hwp_text(path: Path) -> str:
    hwp5txt = shutil.which("hwp5txt")
    if hwp5txt:
        result = subprocess.run([hwp5txt, str(path)], capture_output=True, text=True, timeout=45, check=False)
        text = _normalise_document_text(result.stdout)
        if text:
            return text

    import olefile

    with olefile.OleFileIO(path) as document:
        if not document.exists("FileHeader"):
            return ""
        header = document.openstream("FileHeader").read()
        if len(header) < 40 or not header.startswith(b"HWP Document File"):
            return ""
        flags = int.from_bytes(header[36:40], "little")
        compressed = bool(flags & 0x01)
        stream_names = sorted(
            "/".join(parts)
            for parts in document.listdir()
            if len(parts) == 2 and parts[0] == "BodyText" and parts[1].startswith("Section")
        )
        fragments: list[str] = []
        for stream_name in stream_names:
            section = document.openstream(stream_name).read()
            if compressed:
                try:
                    section = zlib.decompress(section, -15)
                except zlib.error:
                    section = zlib.decompress(section)
            fragments.append(_extract_hwp_records(section))
    return _normalise_document_text("\n".join(fragments))


def _try_ocr_pdf(path: Path) -> str:
    tesseract = shutil.which("tesseract")
    pdftoppm = shutil.which("pdftoppm")
    if not tesseract or not pdftoppm:
        return ""
    with tempfile.TemporaryDirectory(prefix="g2b-ocr-") as directory:
        prefix = Path(directory) / "page"
        render = subprocess.run(
            [pdftoppm, "-png", "-r", "200", str(path), str(prefix)],
            capture_output=True,
            timeout=90,
            check=False,
        )
        if render.returncode != 0:
            return ""
        fragments: list[str] = []
        for image_path in sorted(Path(directory).glob("page-*.png")):
            result = subprocess.run(
                [tesseract, str(image_path), "stdout", "-l", "kor+eng"],
                capture_output=True,
                text=True,
                timeout=90,
                check=False,
            )
            if result.returncode == 0:
                fragments.append(result.stdout)
        return _normalise_document_text("\n".join(fragments))


def _detect_document_suffix(path: Path) -> str:
    """Prefer the downloaded file signature when a G2B filename has a wrong extension."""

    try:
        signature = path.read_bytes()[:8]
    except OSError:
        return path.suffix.lower()
    if signature.startswith(b"%PDF"):
        return ".pdf"
    if signature.startswith(b"PK\x03\x04"):
        try:
            with ZipFile(path) as archive:
                names = set(archive.namelist())
            if "word/document.xml" in names:
                return ".docx"
            if any(name.startswith("Contents/section") and name.endswith(".xml") for name in names):
                return ".hwpx"
            if "xl/sharedStrings.xml" in names or any(name.startswith("xl/worksheets/") for name in names):
                return ".xlsx"
        except BadZipFile:
            return path.suffix.lower()
    if signature.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        # G2B download URLs and file names occasionally omit the .hwp suffix,
        # but the compound-document signature is definitive.
        suffix = path.suffix.lower()
        return suffix if suffix in {".xls", ".doc", ".ppt"} else ".hwp"
    return path.suffix.lower()


def _extract_attachment_text(attachment: LocalAttachmentFile) -> ExtractedAttachment:
    if attachment.extraction_status == "DOWNLOAD_FAILED":
        return ExtractedAttachment(attachment=attachment, text="")
    path = Path(attachment.local_path)
    suffix = _detect_document_suffix(path)
    try:
        if suffix == ".pdf":
            from pypdf import PdfReader

            reader = PdfReader(str(path))
            text = "\n".join(
                f"[page {index + 1}]\n{page.extract_text() or ''}" for index, page in enumerate(reader.pages)
            )
            text = _normalise_document_text(text)
            if not text:
                text = _try_ocr_pdf(path)
            if text:
                updated = attachment.model_copy(
                    update={"extraction_status": "TEXT_EXTRACTED", "extraction_message": "텍스트 추출 완료"}
                )
            else:
                updated = attachment.model_copy(
                    update={"extraction_status": "OCR_REQUIRED", "extraction_message": "스캔 PDF: OCR 확인 필요"}
                )
        elif suffix == ".docx":
            text = _extract_docx_text(path)
            updated = attachment.model_copy(
                update={"extraction_status": "TEXT_EXTRACTED", "extraction_message": "DOCX 텍스트 추출 완료"}
            )
        elif suffix == ".hwpx":
            text = _extract_hwpx_text(path)
            updated = attachment.model_copy(
                update={"extraction_status": "TEXT_EXTRACTED", "extraction_message": "HWPX 텍스트 추출 완료"}
            )
        elif suffix == ".hwp":
            text = _extract_hwp_text(path)
            updated = attachment.model_copy(
                update={"extraction_status": "TEXT_EXTRACTED", "extraction_message": "HWP 텍스트 추출 완료"}
            )
        elif suffix in {".txt", ".csv"}:
            raw = path.read_bytes()
            text = ""
            for encoding in ("utf-8-sig", "cp949", "euc-kr"):
                try:
                    text = raw.decode(encoding)
                    break
                except UnicodeDecodeError:
                    continue
            text = _normalise_document_text(text)
            updated = attachment.model_copy(
                update={"extraction_status": "TEXT_EXTRACTED", "extraction_message": "텍스트 파일 추출 완료"}
            )
        elif suffix in {".xlsx", ".xlsm"}:
            text = _extract_xlsx_text(path)
            updated = attachment.model_copy(
                update={"extraction_status": "TEXT_EXTRACTED", "extraction_message": "스프레드시트 텍스트 추출 완료"}
            )
        elif suffix == ".xls":
            text = _extract_xls_text(path)
            updated = attachment.model_copy(
                update={"extraction_status": "TEXT_EXTRACTED", "extraction_message": "XLS 텍스트 추출 완료"}
            )
        elif suffix == ".zip":
            text = _extract_archive_text(path)
            updated = attachment.model_copy(
                update={"extraction_status": "TEXT_EXTRACTED", "extraction_message": "ZIP 내부 문서 텍스트 추출 완료"}
            )
        else:
            return ExtractedAttachment(
                attachment=attachment.model_copy(
                    update={"extraction_status": "UNSUPPORTED", "extraction_message": "지원하지 않는 파일 형식"}
                ),
                text="",
            )
    except Exception:
        # Individual attachment parser errors (including malformed PDFs) must
        # not block the other selected notices from being reviewed or saved.
        return ExtractedAttachment(
            attachment=attachment.model_copy(
                update={"extraction_status": "EXTRACTION_FAILED", "extraction_message": "파일 텍스트 추출 실패"}
            ),
            text="",
        )
    if not text:
        updated = updated.model_copy(
            update={"extraction_status": "EXTRACTION_FAILED", "extraction_message": "읽을 수 있는 텍스트 없음"}
        )
    return ExtractedAttachment(attachment=updated, text=text)


def _attachment_entries(item: BidNoticePreviewItem) -> tuple[list[tuple[dict[str, Any], str]], list[str]]:
    operations = [("getBidPblancListInfoEorderAtchFileInfo", "e발주 첨부파일")]
    # This attachment operation only applies to 혁신장터 notices. Avoid a
    # slow, unnecessary public-API request for every ordinary notice.
    if "혁신" in (item.business_name or ""):
        operations.append(("getBidPblancListPPIFnlRfpIssAtchFileInfo", "혁신장터 제안요청서"))
    entries: list[tuple[dict[str, Any], str]] = []
    errors: list[str] = []
    seen: set[tuple[str, str]] = set()
    for operation, source_type in operations:
        result, error = _try_fetch(operation, item)
        if error:
            errors.append(error)
            continue
        for entry in result:
            key = (
                _clean_text(entry.get("eorderAtchFileNm") or entry.get("atchFileNm")) or "",
                _clean_text(entry.get("eorderAtchFileUrl") or entry.get("atchFileUrl")) or "",
            )
            if key not in seen:
                seen.add(key)
                entries.append((entry, source_type))
    return entries, _unique(errors)


class _NoticeDetailPageParser(HTMLParser):
    """Read visible detail-page text and attachment-like links without JS."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.text_fragments: list[str] = []
        self.links: list[tuple[str, str]] = []
        self._current_href: str | None = None
        self._current_label: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        values = {name.lower(): value or "" for name, value in attrs}
        self._current_href = values.get("href")
        self._current_label = [values.get("title", ""), values.get("download", "")]

    def handle_data(self, data: str) -> None:
        if data.strip():
            self.text_fragments.append(data)
            if self._current_href is not None:
                self._current_label.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "a" or self._current_href is None:
            return
        self.links.append((self._current_href, " ".join(self._current_label).strip()))
        self._current_href = None
        self._current_label = []


_DOCUMENT_EXTENSION_PATTERN = re.compile(r"\.(?:pdf|hwp|hwpx|doc|docx|xls|xlsx|xlsm|ppt|pptx|txt|csv|zip)(?:$|[?#])", re.IGNORECASE)
_ATTACHMENT_LABEL_PATTERN = re.compile(r"첨부|붙임|공고문|과업|제안요청|제안서|다운로드", re.IGNORECASE)


def _sources_from_detail_page(parser: _NoticeDetailPageParser, page_url: str) -> list[NoticeAttachmentSource]:
    sources: list[NoticeAttachmentSource] = []
    for raw_href, raw_label in parser.links:
        href = raw_href.strip()
        if not href or href.lower().startswith(("javascript:", "mailto:", "#")):
            continue
        url = urljoin(page_url, href)
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            continue
        if not (_DOCUMENT_EXTENSION_PATTERN.search(url) or _ATTACHMENT_LABEL_PATTERN.search(raw_label)):
            continue
        name = _clean_text(raw_label) or _clean_text(Path(parsed.path).name) or "상세 페이지 첨부파일"
        sources.append(
            NoticeAttachmentSource(
                file_name=name,
                download_url=url,
                source_type="나라장터 상세 페이지 첨부파일",
            )
        )
    return _merge_attachment_sources(sources)


def _fetch_notice_detail_page(
    item: BidNoticePreviewItem,
) -> tuple[str, list[NoticeAttachmentSource], str | None]:
    """Read the selected notice's detail page and any download links it exposes."""

    if not item.source_url or urlparse(item.source_url).scheme not in {"http", "https"}:
        return "", [], "나라장터 상세 페이지 URL이 없습니다."
    try:
        response = requests.get(
            item.source_url,
            headers={
                "Accept": "text/html,application/xhtml+xml",
                "User-Agent": "iCore-G2B-Collector/1.0",
            },
            timeout=30,
        )
        response.raise_for_status()
    except requests.RequestException:
        return "", [], "나라장터 상세 페이지를 읽지 못했습니다."

    parser = _NoticeDetailPageParser()
    try:
        parser.feed(response.text)
        parser.close()
    except Exception:
        return "", [], "나라장터 상세 페이지 내용을 해석하지 못했습니다."
    return _normalise_document_text("\n".join(parser.text_fragments)), _sources_from_detail_page(parser, response.url), None


def _fallback_attachment_sources(item: BidNoticePreviewItem) -> tuple[list[NoticeAttachmentSource], list[str]]:
    """Use the public attachment API only if detail-page sources were absent."""

    entries, errors = _attachment_entries(item)
    sources = [
        NoticeAttachmentSource(
            file_name=_attachment_filename(entry),
            download_url=_clean_text(entry.get("eorderAtchFileUrl") or entry.get("atchFileUrl")) or "",
            source_type=source_type,
        )
        for entry, source_type in entries
        if _clean_text(entry.get("eorderAtchFileUrl") or entry.get("atchFileUrl"))
    ]
    return _merge_attachment_sources(sources), errors


def _snippet(text: str, match: re.Match[str], file_name: str) -> str:
    start = max(0, match.start() - 60)
    end = min(len(text), match.end() + 80)
    fragment = _normalise_document_text(text[start:end])
    preceding = text[: match.start()]
    pages = re.findall(r"\[page (\d+)\]", preceding)
    page_suffix = f" p.{pages[-1]}" if pages else ""
    return f"{file_name}{page_suffix}: {fragment}"


def _joint_contracting(extracted: list[ExtractedAttachment], lookup_errors: list[str]) -> EnrichmentCheck:
    readable = [entry for entry in extracted if entry.text]
    if not readable:
        evidence = lookup_errors or ["읽을 수 있는 첨부파일이 없어 공동도급을 확인하지 못함"]
        return EnrichmentCheck(state="REVIEW", label="확인 필요", evidence=evidence)

    term = r"공동(?:수급(?:체)?|도급|이행|계약)"
    negative = re.compile(
        rf"{term}[\s\S]{{0,160}}?(?:불가|불허|허용하지\s*(?:않|아니)|인정하지\s*(?:않|아니))",
        re.IGNORECASE,
    )
    positive = re.compile(
        rf"{term}[\s\S]{{0,160}}?(?:허용(?!하지)|가능|구성(?:할|하여)|공동이행|분담이행)",
        re.IGNORECASE,
    )
    negative_word = re.compile(r"불가|불허|허용하지\s*(?:않|아니)|인정하지\s*(?:않|아니)")
    mention = re.compile(term)
    condition = re.compile(
        r"(?:\d+\s*개(?:사|업체)|\d+\s*%|이하|이상|최소지분|구성원|대표사|분담이행|공동이행|지분율)"
    )
    denied_evidence: list[str] = []
    allowed_evidence: list[str] = []
    allowed_has_condition = False
    for entry in readable:
        matched = negative.search(entry.text)
        if matched:
            denied_evidence.append(_snippet(entry.text, matched, entry.display_name))
        for matched in positive.finditer(entry.text):
            # "공동이행방식은 허용하지 않음" contains an implementation
            # method name that looks positive by itself.  Read the nearby
            # sentence before treating it as permission.
            local_context = entry.text[matched.start() : matched.start() + 220]
            if negative_word.search(local_context):
                continue
            snippet = _snippet(entry.text, matched, entry.display_name)
            allowed_evidence.append(snippet)
            allowed_has_condition = allowed_has_condition or bool(condition.search(snippet))

    if denied_evidence and allowed_evidence:
        return EnrichmentCheck(
            state="REVIEW",
            label="확인 필요 (공동수급 근거 상충)",
            evidence=(allowed_evidence[:2] + denied_evidence[:2]),
        )
    if denied_evidence:
        return EnrichmentCheck(state="NOT_ALLOWED", label="불가", evidence=denied_evidence[:3])
    if allowed_evidence:
        return EnrichmentCheck(
            state="ALLOWED",
            label="가능(조건 있음)" if allowed_has_condition else "가능",
            evidence=allowed_evidence[:3],
        )
    for entry in readable:
        matched = mention.search(entry.text)
        if matched:
            return EnrichmentCheck(state="REVIEW", label="확인 필요", evidence=[_snippet(entry.text, matched, entry.display_name)])
    return EnrichmentCheck(
        state="REVIEW",
        label="확인 필요 (첨부파일에 공동도급 표기 없음)",
        evidence=["첨부파일 텍스트 추출 완료, 공동도급 관련 문구 미발견"],
    )


def _readable_attachments(extracted: list[ExtractedAttachment]) -> list[ExtractedAttachment]:
    return [entry for entry in extracted if entry.text]


def _industry_restriction_from_attachments(extracted: list[ExtractedAttachment]) -> EnrichmentCheck:
    readable = _readable_attachments(extracted)
    if not readable:
        return EnrichmentCheck(state="REVIEW", label="확인 필요", evidence=["읽을 수 있는 첨부파일 없음"])

    no_restriction = re.compile(r"(?:업종|면허)\s*제한.{0,25}(?:없음|없으며|없고|두지 않)")
    restriction = re.compile(r"(?:업종|면허)\s*제한|입찰참가자격")
    # A proposal request can begin an eligibility section with
    # "입찰참가자격" and print the actual industry code several lines later.
    # Look for the explicit code label across the whole document as well.
    labelled_industry_code = re.compile(r"업종\s*코드\s*[:：]?\s*(\d{4})")
    codes: list[str] = []
    evidence: list[str] = []
    no_restriction_evidence: list[str] = []
    for entry in readable:
        for match in restriction.finditer(entry.text):
            snippet = _snippet(entry.text, match, entry.display_name)
            evidence.append(snippet)
            codes.extend(re.findall(r"(?<!\d)(\d{4})(?!\d)", snippet))
        for match in labelled_industry_code.finditer(entry.text):
            snippet = _snippet(entry.text, match, entry.display_name)
            evidence.append(snippet)
            codes.append(match.group(1))
        no_match = no_restriction.search(entry.text)
        if no_match:
            no_restriction_evidence.append(_snippet(entry.text, no_match, entry.display_name))
    codes = _unique(codes)
    allowed = [code for code in codes if code in ALLOWED_INDUSTRY_CODES]
    if codes and no_restriction_evidence:
        return EnrichmentCheck(
            state="REVIEW",
            label="확인 필요 (업종제한 근거 상충)",
            evidence=(evidence[:2] + no_restriction_evidence[:2]),
        )
    if allowed:
        return EnrichmentCheck(state="PASS", label=f"통과: {', '.join(allowed)}", evidence=evidence[:3])
    if codes:
        return EnrichmentCheck(state="FAIL", label=f"불일치: {', '.join(codes)}", evidence=evidence[:3])
    if no_restriction_evidence:
        return EnrichmentCheck(state="NO_RESTRICTION", label="제한 없음", evidence=no_restriction_evidence[:3])
    return EnrichmentCheck(state="REVIEW", label="확인 필요", evidence=evidence[:3] or ["업종제한 문구 미발견"])


def _region_restriction_from_attachments(extracted: list[ExtractedAttachment]) -> EnrichmentCheck:
    readable = _readable_attachments(extracted)
    if not readable:
        return EnrichmentCheck(state="REVIEW", label="확인 필요", evidence=["읽을 수 있는 첨부파일 없음"])

    no_restriction = re.compile(
        r"(?:지역\s*제한|주된\s*영업소|소재지|본점|사업장).{0,35}?"
        r"(?:없음|없으며|없고|두지\s*않|제한하지\s*않|전국)"
    )
    notice_reference = re.compile(r"지역\s*제한[\s\S]{0,50}?공고서\s*참조")
    explicit_restriction = re.compile(r"지역\s*제한|(?:주된\s*영업소|소재지|본점|사업장)\s*제한")
    evidence: list[str] = []
    reference_evidence: list[str] = []
    no_restriction_evidence: list[str] = []
    restricted_regions: list[str] = []
    for entry in readable:
        no_match = no_restriction.search(entry.text)
        if no_match:
            no_restriction_evidence.append(_snippet(entry.text, no_match, entry.display_name))
        reference_match = notice_reference.search(entry.text)
        if reference_match:
            reference_evidence.append(_snippet(entry.text, reference_match, entry.display_name))
            continue
        for match in explicit_restriction.finditer(entry.text):
            # Do not count the ``지역제한 없음`` phrase itself as both a
            # positive and negative rule.  A separate positive condition in
            # the same document is still collected and becomes a conflict.
            if no_match and no_match.start() <= match.start() < no_match.end():
                continue
            evidence.append(_snippet(entry.text, match, entry.display_name))
            restricted_regions.extend(_region_names_in_text(_snippet(entry.text, match, "")))

        # Example: "사업자의 소재지가 경상북도에 있는 업체".  The region
        # name alone is not sufficient: it must be in the same local context
        # as a registered-address field and an eligibility condition.
        for match in RESIDENCY_FIELD_PATTERN.finditer(entry.text):
            start = max(0, match.start() - 80)
            end = min(len(entry.text), match.end() + 180)
            context = entry.text[start:end]
            regions = _region_names_in_text(context)
            if regions and RESIDENCY_ELIGIBILITY_PATTERN.search(context):
                evidence.append(_snippet(entry.text, match, entry.display_name))
                restricted_regions.extend(regions)
    if evidence and no_restriction_evidence:
        return EnrichmentCheck(
            state="REVIEW",
            label="확인 필요 (지역제한 근거 상충)",
            evidence=(evidence[:2] + no_restriction_evidence[:2]),
        )
    if evidence:
        regions = _unique(restricted_regions)
        label = f"제한: {', '.join(regions)}" if regions else "제한 있음"
        return EnrichmentCheck(state="PASS", label=label, evidence=_unique(evidence)[:3])
    if no_restriction_evidence:
        return EnrichmentCheck(state="NO_RESTRICTION", label="제한 없음", evidence=no_restriction_evidence[:3])
    if reference_evidence:
        return EnrichmentCheck(
            state="REVIEW",
            label="확인 필요 (공고서 참조)",
            evidence=reference_evidence[:3],
        )
    return EnrichmentCheck(state="REVIEW", label="확인 필요", evidence=["지역제한 문구 미발견"])


def _combine_structured_and_document_checks(
    field_name: str,
    structured: EnrichmentCheck,
    documents: EnrichmentCheck,
) -> EnrichmentCheck:
    """Prefer an official structured field, but never hide contradictory evidence."""

    decisive_states = {"PASS", "FAIL", "NO_RESTRICTION", "ALLOWED", "NOT_ALLOWED"}
    structured_is_decisive = structured.state in decisive_states
    documents_is_decisive = documents.state in decisive_states
    if structured_is_decisive and documents_is_decisive and structured.state != documents.state:
        return EnrichmentCheck(
            state="REVIEW",
            label=f"확인 필요 ({field_name} 근거 상충)",
            evidence=(structured.evidence[:2] + documents.evidence[:2]),
        )
    if structured_is_decisive:
        return structured
    if documents_is_decisive:
        return documents
    if structured.evidence:
        return structured
    return documents


def _region_restriction_from_structured_api(
    items: list[dict[str, Any]], error: str | None
) -> EnrichmentCheck:
    """An empty regional list is not enough to override a '공고서 참조' notice."""

    if error:
        return _region_restriction(items, error)
    if not items:
        return EnrichmentCheck(
            state="REVIEW",
            label="확인 필요 (상세 지역제한 항목 없음)",
            evidence=["나라장터 참가가능지역정보: 반환 항목 없음"],
        )
    return _region_restriction(items, None)


def _enrich_one(item: BidNoticePreviewItem) -> BidNoticePreviewItem:
    license_rows, license_error = _try_fetch("getBidPblancListInfoLicenseLimit", item)
    region_rows, region_error = _try_fetch(
        "getBidPblancListInfoPrtcptPsblRgn",
        item,
        allow_unmatched_result=True,
    )
    detail_text, detail_sources, detail_error = _fetch_notice_detail_page(item)
    sources = _merge_attachment_sources(item.attachment_sources, detail_sources)
    fallback_errors: list[str] = []
    if not sources:
        fallback_sources, fallback_errors = _fallback_attachment_sources(item)
        sources = _merge_attachment_sources(sources, fallback_sources)

    downloaded_attachments = [_download_attachment(item, source) for source in sources]
    downloaded_paths = {Path(attachment.local_path).resolve() for attachment in downloaded_attachments}
    cached_attachments = [
        attachment
        for attachment in _cached_attachments(item)
        if Path(attachment.local_path).resolve() not in downloaded_paths
    ]
    attachments_to_analyse = [*downloaded_attachments, *cached_attachments]
    extracted = [_extract_attachment_text(attachment) for attachment in attachments_to_analyse]
    analysis_sources = list(extracted)
    if detail_text:
        analysis_sources.insert(
            0,
            ExtractedAttachment(
                attachment=None,
                source_name="나라장터 공고 상세 페이지",
                text=detail_text,
            ),
        )

    industry_restriction = _combine_structured_and_document_checks(
        "업종제한",
        _industry_restriction(license_rows, license_error),
        _industry_restriction_from_attachments(analysis_sources),
    )
    region_restriction = _combine_structured_and_document_checks(
        "지역제한",
        _region_restriction_from_structured_api(region_rows, region_error),
        _region_restriction_from_attachments(analysis_sources),
    )
    lookup_errors = [error for error in [detail_error, *fallback_errors] if error]
    if attachments_to_analyse:
        attachment_label = f"상세 페이지 및 첨부파일 {len(attachments_to_analyse)}개 분석 완료"
    else:
        attachment_label = lookup_errors[0] if lookup_errors else "상세 페이지에서 첨부파일을 찾지 못했습니다."
    return item.model_copy(
        update={
            "industry_restriction": industry_restriction,
            "region_restriction_detail": region_restriction,
            "joint_contracting": _joint_contracting(
                analysis_sources,
                lookup_errors or [item.attachment_lookup_label],
            ),
            "attachment_sources": sources,
            "attachment_lookup_label": attachment_label,
            "attachments": [entry.attachment for entry in extracted if entry.attachment is not None],
            "enrichment_checked_at": datetime.now(KST),
            "detail_enrichment_status": "DETAIL_COMPLETED",
            "common_storage_record": item.common_storage_record.model_copy(
                update={
                    "region_restriction": True
                    if region_restriction.state == "PASS"
                    else False
                    if region_restriction.state == "NO_RESTRICTION"
                    else item.common_storage_record.region_restriction
                }
            ),
        }
    )


def enrich_bid_notice_items(items: list[BidNoticePreviewItem]) -> list[BidNoticePreviewItem]:
    """Download and inspect every notice included in a collection preview."""

    if not items:
        raise G2BEnrichmentError("상세 확인할 공고가 없습니다.")

    enriched_items: list[BidNoticePreviewItem] = []
    for item in items:
        try:
            enriched_items.append(_enrich_one(item))
        except Exception as error:
            # One malformed detail page or attachment must not hide all other
            # collected notices.  Preserve the notice in the preview and make
            # the failed analysis explicit for all three derived fields.
            evidence = [f"상세·첨부파일 분석 실패: {str(error)[:240]}"]
            failed_check = EnrichmentCheck(
                state="REVIEW",
                label="확인 필요 (분석 오류)",
                evidence=evidence,
            )
            enriched_items.append(
                item.model_copy(
                    update={
                        "industry_restriction": failed_check,
                        "joint_contracting": failed_check,
                        "region_restriction_detail": failed_check,
                        "attachment_lookup_label": evidence[0],
                        "enrichment_checked_at": datetime.now(KST),
                        "detail_enrichment_status": "SOURCE_MISSING",
                    }
                )
            )
    return enriched_items
