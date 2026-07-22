from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.data.models import ScraperNoticeModel
from app.g2b.bid_notice import canonical_bid_notice_identity
from app.g2b.opening_results.schemas import BidNoticeSheetContext


class AmbiguousBidNoticeContextError(RuntimeError):
    def __init__(self, context_keys: list[str]) -> None:
        self.context_keys = context_keys
        super().__init__("동일한 공고번호와 차수의 입찰공고가 여러 건입니다.")


def canonical_notice_key(
    bid_notice_no: str,
    bid_notice_ord: str | None,
) -> tuple[str, str]:
    identity = canonical_bid_notice_identity(bid_notice_no, bid_notice_ord)
    if identity is None:
        raise ValueError("bid_notice_no is required")
    return identity


def load_bid_notice_contexts(
    db: Session,
    requested_keys: Iterable[tuple[str, str]],
) -> dict[tuple[str, str], BidNoticeSheetContext]:
    contexts, ambiguous_keys = resolve_bid_notice_contexts(db, requested_keys)
    if ambiguous_keys:
        raise AmbiguousBidNoticeContextError(
            [f"{notice_no}|{notice_ord}" for notice_no, notice_ord in sorted(ambiguous_keys)]
        )
    return contexts


def resolve_bid_notice_contexts(
    db: Session,
    requested_keys: Iterable[tuple[str, str]],
) -> tuple[
    dict[tuple[str, str], BidNoticeSheetContext],
    set[tuple[str, str]],
]:
    canonical_keys = {
        canonical_notice_key(bid_notice_no, bid_notice_ord)
        for bid_notice_no, bid_notice_ord in requested_keys
    }
    if not canonical_keys:
        return {}, set()

    notice_numbers = {key[0] for key in canonical_keys}
    records = (
        db.execute(
            select(ScraperNoticeModel).where(
                ScraperNoticeModel.bid_notice_no.in_(notice_numbers)
            )
        )
        .scalars()
        .all()
    )

    contexts: dict[tuple[str, str], BidNoticeSheetContext] = {}
    ambiguous_keys: set[tuple[str, str]] = set()
    for record in records:
        if not record.bid_notice_no:
            continue
        key = canonical_notice_key(record.bid_notice_no, record.bid_notice_ord)
        if key not in canonical_keys:
            continue
        if key in ambiguous_keys:
            continue
        if key in contexts:
            ambiguous_keys.add(key)
            contexts.pop(key, None)
            continue
        contexts[key] = BidNoticeSheetContext(
            bid_notice_no=record.bid_notice_no.strip(),
            bid_notice_ord=(record.bid_notice_ord or "00").strip(),
            business_name=record.business_name,
            demand_agency_name=record.demand_agency_name,
            base_amount=record.base_amount,
            prearranged_price_decision_method=(
                record.prearranged_price_decision_method
            ),
            proposal_deadline=record.proposal_deadline,
            region_restriction=record.region_restriction,
            region_restriction_api_status=record.region_restriction_api_status,
            is_two_stage_bid=record.is_two_stage_bid,
        )

    return contexts, ambiguous_keys
