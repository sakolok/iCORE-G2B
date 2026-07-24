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


def select_canonical_scraper_notice(
    records: Iterable[ScraperNoticeModel],
) -> ScraperNoticeModel | None:
    """명확히 최신·완전한 과거 중복 행만 안전하게 통합한다."""
    candidates = list(records)
    if not candidates:
        return None

    def completeness(record: ScraperNoticeModel) -> int:
        fields = (
            record.business_name,
            record.demand_agency_name,
            record.base_amount,
            record.prearranged_price_decision_method,
            record.proposal_deadline,
            record.region_restriction,
            record.is_two_stage_bid,
            record.notice_url,
        )
        return sum(value is not None and str(value).strip() != "" for value in fields)

    def rank(record: ScraperNoticeModel) -> int:
        return completeness(record)

    best_rank = max(rank(record) for record in candidates)
    best = [record for record in candidates if rank(record) == best_rank]
    # 서로 같은 정보량이면 어느 행이 공식 원본인지 판단할 수 없다.
    if len(best) != 1:
        return None
    return best[0]


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

    records_by_key: dict[tuple[str, str], list[ScraperNoticeModel]] = {}
    for record in records:
        if not record.bid_notice_no:
            continue
        key = canonical_notice_key(record.bid_notice_no, record.bid_notice_ord)
        if key not in canonical_keys:
            continue
        records_by_key.setdefault(key, []).append(record)

    contexts: dict[tuple[str, str], BidNoticeSheetContext] = {}
    ambiguous_keys: set[tuple[str, str]] = set()
    for key, candidates in records_by_key.items():
        record = select_canonical_scraper_notice(candidates)
        if record is None:
            ambiguous_keys.add(key)
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
            notice_url=record.notice_url,
        )

    # 동일한 공고번호·차수의 과거 중복은 더 완전한 행이 명확할 때만
    # 논리 통합한다. 동률이면 기존처럼 공고정보 중복으로 안전하게 막는다.
    return contexts, ambiguous_keys
