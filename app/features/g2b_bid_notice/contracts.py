"""Storage contract for the G2B collector feature.

This module deliberately contains no SQLAlchemy models or migrations.  The
integration branch owns the shared database and exposes the shared save
functions; this feature only produces records in that function's contract.
"""

from collections.abc import Mapping
from datetime import datetime
import re
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel


KST = ZoneInfo("Asia/Seoul")


class BidNoticeStorageRecord(BaseModel):
    """Exact bid-notice payload passed to the common storage function.

    Values are nullable on purpose.  A collector must not manufacture a value
    when the G2B response has not supplied the corresponding datum.
    """

    bid_notice_no: str | None = None
    bid_notice_ord: str | None = None
    business_name: str | None = None
    demand_agency_name: str | None = None
    base_amount: int | None = None
    proposal_deadline: datetime | None = None
    region_restriction: bool | None = None
    is_two_stage_bid: bool | None = None


class PreSpecificationStorageRecord(BaseModel):
    """Exact pre-specification identity/link payload for future collection."""

    bfSpecRgstNo: str | None = None
    bid_notice_no: str | None = None
    bid_notice_ord: str | None = None


def _clean_text(value: object) -> str | None:
    text = str("" if value is None else value).strip()
    return text or None


def _first_value(source: Mapping[str, Any], *field_names: str) -> object | None:
    for field_name in field_names:
        value = source.get(field_name)
        if value is not None and str(value).strip() != "":
            return value
    return None


def _parse_amount(value: object) -> int | None:
    text = _clean_text(value)
    if text is None:
        return None
    if not re.fullmatch(r"[0-9,]+", text):
        return None
    return int(text.replace(",", ""))


def _parse_kst_datetime(value: object) -> datetime | None:
    if value is None or str(value).strip() == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip().replace("Z", "+00:00")
        parsed = None
        for format_string in ("%Y%m%d%H%M", "%Y%m%d%H%M%S", "%Y%m%d"):
            try:
                parsed = datetime.strptime(text, format_string)
                break
            except ValueError:
                continue
        if parsed is None:
            try:
                parsed = datetime.fromisoformat(text)
            except ValueError:
                return None

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=KST)
    return parsed.astimezone(KST)


def _parse_bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    text = _clean_text(value)
    if text is None:
        return None
    normalized = text.upper()
    if normalized in {"Y", "YES", "TRUE", "1"}:
        return True
    if normalized in {"N", "NO", "FALSE", "0"}:
        return False
    return None


def canonical_bid_notice_ord(bid_notice_ord: str | None) -> str | None:
    """Return an ordinal used only for duplicate comparison.

    The raw ordinal is always retained in ``BidNoticeStorageRecord``.  Numeric
    ordinals are compared without leading zeroes, so ``00`` and ``000`` are
    considered the same ordinal as required by the integration contract.
    """

    ordinal = _clean_text(bid_notice_ord)
    if ordinal is None:
        return None
    if re.fullmatch(r"[0-9]+", ordinal):
        return str(int(ordinal))
    return ordinal


def bid_notice_dedup_key(
    bid_notice_no: str | None, bid_notice_ord: str | None
) -> tuple[str, str] | None:
    """Build the comparison key without changing the raw strings to store."""

    notice_no = _clean_text(bid_notice_no)
    canonical_ord = canonical_bid_notice_ord(bid_notice_ord)
    if notice_no is None or canonical_ord is None:
        return None
    return notice_no, canonical_ord


def pre_spec_dedup_key(bf_spec_rgst_no: str | None) -> str | None:
    """Return the pre-specification identity exactly as received, or ``None``."""

    return _clean_text(bf_spec_rgst_no)


def map_bid_notice_api_item(source: Mapping[str, Any]) -> BidNoticeStorageRecord:
    """Map direct G2B values to the common bid-notice storage contract.

    ``presmptPrce``/``asignBdgtAmt`` (estimated price/budget) are deliberately
    not used as ``base_amount``.  Likewise, ``bidClseDt`` is a bid closing time,
    not a proposal deadline.  The record keeps those contract fields null until
    the dedicated G2B detail endpoints provide direct values.
    """

    return BidNoticeStorageRecord(
        bid_notice_no=_clean_text(_first_value(source, "bid_notice_no", "bidNtceNo")),
        bid_notice_ord=_clean_text(_first_value(source, "bid_notice_ord", "bidNtceOrd")),
        business_name=_clean_text(
            _first_value(source, "business_name", "businessName", "bidNtceNm")
        ),
        demand_agency_name=_clean_text(
            _first_value(source, "demand_agency_name", "demandAgencyName", "dminsttNm")
        ),
        base_amount=_parse_amount(_first_value(source, "base_amount", "baseAmount", "bsisAmount")),
        proposal_deadline=_parse_kst_datetime(
            _first_value(
                source,
                "proposal_deadline",
                "proposalDeadline",
                "prpslSbmtnEndDt",
                "prpslSbtEndDt",
            )
        ),
        region_restriction=_parse_bool(
            _first_value(
                source,
                "region_restriction",
                "regionRestriction",
                "isRegionRestriction",
                "rgnLmtYn",
            )
        ),
        is_two_stage_bid=_parse_bool(
            _first_value(source, "is_two_stage_bid", "isTwoStageBid", "twoStageBidYn")
        ),
    )


def map_pre_spec_api_item(source: Mapping[str, Any]) -> PreSpecificationStorageRecord:
    """Map a pre-specification without guessing a later bid-notice link."""

    return PreSpecificationStorageRecord(
        bfSpecRgstNo=_clean_text(_first_value(source, "bfSpecRgstNo")),
        bid_notice_no=_clean_text(_first_value(source, "bid_notice_no", "bidNtceNo")),
        bid_notice_ord=_clean_text(_first_value(source, "bid_notice_ord", "bidNtceOrd")),
    )
