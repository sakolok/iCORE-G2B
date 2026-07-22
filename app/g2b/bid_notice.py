from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal
from zoneinfo import ZoneInfo


KST = ZoneInfo("Asia/Seoul")

REGION_API_VALUE = "API_VALUE"
REGION_API_EMPTY = "API_EMPTY"
REGION_API_ERROR = "API_ERROR"
REGION_API_ORDER_MISMATCH = "ORDER_MISMATCH"
RegionRestrictionApiStatus = Literal[
    "API_VALUE",
    "API_EMPTY",
    "API_ERROR",
    "ORDER_MISMATCH",
]


def clean_optional_text(value: Any, *, max_length: int | None = None) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    return text[:max_length] if max_length is not None else text


def canonical_bid_notice_order(bid_notice_ord: Any) -> str:
    notice_ord = clean_optional_text(bid_notice_ord) or "00"
    return str(int(notice_ord)) if notice_ord.isdigit() else notice_ord


def canonical_bid_notice_identity(
    bid_notice_no: Any,
    bid_notice_ord: Any,
) -> tuple[str, str] | None:
    notice_no = clean_optional_text(bid_notice_no)
    if notice_no is None:
        return None
    return notice_no, canonical_bid_notice_order(bid_notice_ord)


def parse_official_amount(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return value
    normalized = str(value).strip().replace(",", "")
    if not normalized:
        return None
    try:
        return Decimal(normalized)
    except InvalidOperation:
        return None


def parse_business_amount(item: dict[str, Any]) -> Decimal | None:
    """사업금액은 공식 추정가격과 부가세의 합으로만 계산한다."""
    explicit_amount = parse_official_amount(item.get("business_amount"))
    if explicit_amount is not None:
        return explicit_amount

    estimated_price = parse_official_amount(
        item.get("presmptPrce")
        if item.get("presmptPrce") is not None
        else item.get("estimated_price")
    )
    vat = parse_official_amount(
        item.get("VAT") if item.get("VAT") is not None else item.get("vat")
    )
    if estimated_price is None or vat is None:
        return None
    return estimated_price + vat


def parse_g2b_datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=KST) if value.tzinfo is None else value.astimezone(KST)

    text = str(value).strip()
    formats = {
        14: "%Y%m%d%H%M%S",
        12: "%Y%m%d%H%M",
        8: "%Y%m%d",
    }
    if text.isdigit() and len(text) in formats:
        try:
            return datetime.strptime(text, formats[len(text)]).replace(tzinfo=KST)
        except ValueError:
            return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=KST) if parsed.tzinfo is None else parsed.astimezone(KST)


def parse_optional_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    normalized = str(value).strip().lower()
    if normalized in {"y", "yes", "true", "1", "예", "해당"}:
        return True
    if normalized in {"n", "no", "false", "0", "아니오", "비해당"}:
        return False
    return None


def infer_two_stage_bid(explicit_value: Any, *official_method_values: Any) -> bool | None:
    explicit = parse_optional_bool(explicit_value)
    if explicit is not None:
        return explicit

    methods = [str(value).strip() for value in official_method_values if str(value or "").strip()]
    if not methods:
        return None
    normalized = " ".join(methods).replace(" ", "").replace("·", "")
    known_two_stage_markers = (
        "2단계",
        "규격가격동시",
    )
    if any(marker in normalized for marker in known_two_stage_markers):
        return True
    known_non_two_stage_markers = (
        "협상에의한계약",
        "적격심사",
        "최저가",
        "최고가",
        "수의계약",
        "종합평가",
        "종합심사",
        "희망수량",
    )
    if any(marker in normalized for marker in known_non_two_stage_markers):
        return False
    return None


def missing_bid_notice_context_fields(notice: Any) -> list[str]:
    missing: list[str] = []
    if canonical_bid_notice_identity(
        getattr(notice, "bid_notice_no", None),
        getattr(notice, "bid_notice_ord", None),
    ) is None:
        missing.append("bid_notice_no")
    for field_name in ("business_name", "demand_agency_name", "region_restriction"):
        value = getattr(notice, field_name, None)
        if not isinstance(value, str) or not value.strip():
            missing.append(field_name)
    region_value = clean_optional_text(getattr(notice, "region_restriction", None))
    region_api_status = getattr(notice, "region_restriction_api_status", None)
    if (
        region_api_status
        in {REGION_API_EMPTY, REGION_API_ERROR, REGION_API_ORDER_MISMATCH}
        or (region_api_status is None and region_value == "없음")
    ) and "region_restriction" not in missing:
        missing.append("region_restriction")
    if getattr(notice, "base_amount", None) is None:
        missing.append("base_amount")
    for field_name in ("proposal_deadline", "is_two_stage_bid"):
        if getattr(notice, field_name, None) is None:
            missing.append(field_name)
    return missing
