"""Column-specific G2B bid-notice classifier for personal collection settings."""

from datetime import date
import re

from app.features.g2b_bid_notice.schemas import (
    BidNoticeCandidate,
    BidNoticePreviewItem,
    ColumnDecision,
    PersonalCollectionSettings,
)


def _normalize(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().lower()


def _active_amount_range(settings: PersonalCollectionSettings) -> bool:
    return settings.base_amount_min is not None or settings.base_amount_max is not None


def _active_posted_date_range(settings: PersonalCollectionSettings) -> bool:
    return settings.posted_date_start is not None or settings.posted_date_end is not None


def _organization_decision(settings: PersonalCollectionSettings, candidate: BidNoticeCandidate) -> ColumnDecision:
    if not settings.organization_groups:
        return ColumnDecision(column="수요기관", status="INACTIVE", detail="대상 기관 그룹 조건 비활성")

    agency_name = _normalize(candidate.demand_agency_name)
    agency_code = _normalize(candidate.demand_agency_code)
    for group in settings.organization_groups:
        names = {
            _normalize(value)
            for value in [*group.parent_agencies, *group.child_agencies, *group.aliases]
            if _normalize(value)
        }
        codes = {_normalize(value) for value in group.agency_codes if _normalize(value)}
        if agency_name in names or (agency_code and agency_code in codes):
            return ColumnDecision(
                column="수요기관",
                status="PASS",
                detail=f"{candidate.demand_agency_name or candidate.demand_agency_code} → {group.name} 그룹 일치",
            )

    return ColumnDecision(
        column="수요기관",
        status="FAIL",
        detail=f"{candidate.demand_agency_name or candidate.demand_agency_code or '원본값 없음'}이(가) 선택 기관 목록과 불일치",
    )


def _metadata_decision(
    *,
    column: str,
    selected_values: list[str],
    raw_value: str | None,
) -> ColumnDecision:
    if not selected_values:
        return ColumnDecision(column=column, status="INACTIVE", detail=f"{column} 조건 비활성")
    if raw_value is None:
        return ColumnDecision(column=column, status="PENDING", detail=f"{column} 원본값 확인 필요")
    if raw_value in selected_values:
        return ColumnDecision(column=column, status="PASS", detail=f"{raw_value} 일치")
    if column == "업무구분" and raw_value == "용역" and {"일반용역", "기술용역"}.intersection(selected_values):
        return ColumnDecision(
            column=column,
            status="PENDING",
            detail="목록 API에서 일반용역·기술용역 구분을 제공하지 않아 상세 확인 필요",
        )
    return ColumnDecision(column=column, status="FAIL", detail=f"{raw_value} 불일치")


def _title_keyword_decisions(
    settings: PersonalCollectionSettings, candidate: BidNoticeCandidate
) -> list[ColumnDecision]:
    title = _normalize(candidate.bid_ntce_nm)
    required_keywords = [keyword.strip() for keyword in settings.required_title_keywords if keyword.strip()]
    excluded_keywords = [keyword.strip() for keyword in settings.excluded_title_keywords if keyword.strip()]
    missing_keywords = [keyword for keyword in required_keywords if _normalize(keyword) not in title]
    present_excluded = [keyword for keyword in excluded_keywords if _normalize(keyword) in title]

    required = (
        ColumnDecision(column="공고명 필수 키워드", status="INACTIVE", detail="필수 키워드 조건 비활성")
        if not required_keywords
        else ColumnDecision(
            column="공고명 필수 키워드",
            status="FAIL" if missing_keywords else "PASS",
            detail=(f"공고명에 누락: {', '.join(missing_keywords)}" if missing_keywords else f"{', '.join(required_keywords)} 모두 포함"),
        )
    )
    excluded = (
        ColumnDecision(column="공고명 제외 키워드", status="INACTIVE", detail="제외 키워드 조건 비활성")
        if not excluded_keywords
        else ColumnDecision(
            column="공고명 제외 키워드",
            # A notice containing both the required and excluded terms can be
            # a relevant project with an ambiguous title.  Keep it out of the
            # automatic priority list, but let the user inspect it in REVIEW.
            status="PENDING" if present_excluded and not missing_keywords else "FAIL" if present_excluded else "PASS",
            detail=(
                f"필수 키워드는 일치하지만 제외 키워드 포함: {', '.join(present_excluded)} → 확인 필요"
                if present_excluded and not missing_keywords
                else f"{', '.join(present_excluded)} 포함"
                if present_excluded
                else "제외 키워드 미포함"
            ),
        )
    )
    return [required, excluded]


def _base_amount_decision(settings: PersonalCollectionSettings, candidate: BidNoticeCandidate) -> ColumnDecision:
    if not _active_amount_range(settings):
        return ColumnDecision(column="기초금액", status="INACTIVE", detail="금액 조건 비활성")
    if candidate.base_amount is None:
        return ColumnDecision(column="기초금액", status="PENDING", detail="상세 확인 필요 (원본값 없음)")
    below_minimum = settings.base_amount_min is not None and candidate.base_amount < settings.base_amount_min
    above_maximum = settings.base_amount_max is not None and candidate.base_amount > settings.base_amount_max
    return ColumnDecision(
        column="기초금액",
        status="FAIL" if below_minimum or above_maximum else "PASS",
        detail="설정한 금액 범위 불일치" if below_minimum or above_maximum else "설정한 금액 범위 일치",
    )


def _region_decision(settings: PersonalCollectionSettings, candidate: BidNoticeCandidate) -> ColumnDecision:
    if not settings.participation_regions:
        return ColumnDecision(column="참가 가능 지역", status="INACTIVE", detail="지역 조건 비활성")
    if candidate.participation_regions is None:
        return ColumnDecision(column="참가 가능 지역", status="PENDING", detail="상세 확인 필요 (지역제한 원본값 없음)")
    matches = "전국" in candidate.participation_regions or bool(
        set(settings.participation_regions).intersection(candidate.participation_regions)
    )
    return ColumnDecision(
        column="참가 가능 지역",
        status="PASS" if matches else "FAIL",
        detail="원본값: 전국" if "전국" in candidate.participation_regions else ("선택 지역 일치" if matches else "선택 지역과 불일치"),
    )


def _posted_date_decision(
    settings: PersonalCollectionSettings, candidate: BidNoticeCandidate
) -> ColumnDecision:
    if not _active_posted_date_range(settings):
        return ColumnDecision(column="게시일자", status="INACTIVE", detail="게시일자 조건 비활성")
    if candidate.published_at is None:
        return ColumnDecision(column="게시일자", status="PENDING", detail="원본 게시일시 확인 필요")
    posted_date: date = candidate.published_at.date()
    before_start = settings.posted_date_start is not None and posted_date < settings.posted_date_start
    after_end = settings.posted_date_end is not None and posted_date > settings.posted_date_end
    return ColumnDecision(
        column="게시일자",
        status="FAIL" if before_start or after_end else "PASS",
        detail="설정한 기간과 불일치" if before_start or after_end else "설정한 기간 일치",
    )


def classify_bid_notice(
    settings: PersonalCollectionSettings, candidate: BidNoticeCandidate
) -> tuple[str, list[ColumnDecision]]:
    """Classify using independent source columns; every active condition is AND."""

    decisions = [
        _organization_decision(settings, candidate),
        _metadata_decision(column="업무구분", selected_values=settings.work_types, raw_value=candidate.work_type),
        _metadata_decision(column="조달 구분", selected_values=settings.procurement_types, raw_value=candidate.procurement_type),
        *_title_keyword_decisions(settings, candidate),
        _base_amount_decision(settings, candidate),
        _region_decision(settings, candidate),
        _posted_date_decision(settings, candidate),
    ]
    if any(decision.status == "FAIL" for decision in decisions):
        return "EXCLUDE", decisions
    if any(decision.status == "PENDING" for decision in decisions):
        return "REVIEW", decisions
    return "PRIORITY", decisions


def apply_industry_restriction_exclusion(item: BidNoticePreviewItem) -> BidNoticePreviewItem:
    """Move a notice with a confirmed industry-code mismatch to EXCLUDE.

    The initial list API does not include the detailed industry restriction.
    This rule therefore runs only after the detail page and attachments have
    been analysed.  Unconfirmed and unavailable values deliberately remain in
    REVIEW instead of being excluded.
    """

    # `FAIL` is the normal structured result.  The label check makes the
    # final exclusion invariant resilient when a merged source preserves the
    # user-facing "불일치" result but represents its state as REVIEW.
    restriction_label = item.industry_restriction.label or ""
    is_code_mismatch = (
        item.industry_restriction.state == "FAIL"
        or "불일치" in restriction_label
    )
    if not is_code_mismatch:
        return item

    decisions = [
        decision
        for decision in item.column_decisions
        if decision.column != "업종제한(기관코드)"
    ]
    decisions.append(
        ColumnDecision(
            column="업종제한(기관코드)",
            status="FAIL",
            detail=item.industry_restriction.label,
        )
    )
    return item.model_copy(
        update={
            "match_status": "EXCLUDE",
            "column_decisions": decisions,
        }
    )
