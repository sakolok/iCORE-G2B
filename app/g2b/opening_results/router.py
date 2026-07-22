import hmac
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.data.database import get_db
from app.g2b.bid_notice import missing_bid_notice_context_fields
from app.g2b.opening_results.client import (
    OpeningResultApiConfigurationError,
    OpeningResultApiError,
)
from app.g2b.opening_results.schemas import (
    CollectOpeningResultsRequest,
    CollectOpeningResultsResponse,
    DismissOpeningResultResponse,
    ExportOpeningResultsSheetRequest,
    ExportOpeningResultsSheetResponse,
    OpeningEntryResponse,
    OpeningResultDetailResponse,
    OpeningResultListQuery,
    OpeningResultListResponse,
    OpeningResultProfileResponse,
    OpeningResultProfileUpdateRequest,
    OpeningResultSettingsResponse,
    OpeningResultSummaryResponse,
    OpeningStatus,
    RestoreOpeningResultResponse,
    ScheduledCollectOpeningResultsResponse,
    SheetDestinationResponse,
    SheetDestinationUpsertRequest,
    SheetDestinationVerifyRequest,
    SheetDestinationVerifyResponse,
)
from app.g2b.keyword_policy import normalize_keywords
from app.g2b.opening_results.matching import (
    ResultAccessError,
    SheetDestinationAccessError,
    SheetDestinationConflictError,
    SheetExportConflictError,
    claim_sheet_exports,
    complete_sheet_exports,
    deactivate_sheet_destination,
    dismiss_result,
    ensure_sheet_target_access,
    fail_sheet_exports,
    get_user_result_profile,
    list_sheet_destinations,
    load_visible_results,
    normalize_spreadsheet_id,
    resolve_sheet_destination,
    restore_dismissed_result,
    save_sheet_destination,
    update_user_result_profile,
)
from app.g2b.opening_results.models import BidOpeningEntryModel
from app.g2b.opening_results.notice_context_repository import (
    AmbiguousBidNoticeContextError,
    canonical_notice_key,
    resolve_bid_notice_contexts,
)
from app.g2b.opening_results.service import (
    collect_opening_results,
    get_opening_result,
    list_opening_results,
    OpeningResultCollectionConflictError,
    OpeningResultCollectionLeaseLostError,
    run_scheduled_opening_results,
)
from app.g2b.opening_results.sheet_export import (
    GoogleSheetWriter,
    SHEET_HEADERS,
    SheetExportConfigurationError,
    build_sheet_preview_token,
    build_sheet_rows,
    find_duplicate_notice_numbers,
    get_sheet_service_account_email,
    organize_entry_rankings,
)
from app.services.auth_service import (
    require_organization_auth,
    verify_cloud_scheduler_oidc_token,
    verify_scraper_internal_token,
)


router = APIRouter(prefix="/api/v1/results", tags=["g2b-results"])


def _can_manage_organization(auth: dict) -> bool:
    return auth.get("organization_role") == "admin" or auth.get("role") == "admin"


def _destination_response(destination) -> SheetDestinationResponse:
    return SheetDestinationResponse(
        id=destination.id,
        label=destination.label,
        spreadsheet_id=destination.spreadsheet_id,
        tab_name=destination.tab_name,
        scope="PERSONAL" if destination.owner_user_id is not None else "ORGANIZATION",
        is_default=destination.is_default,
    )


def _summary_responses(
    db: Session,
    rows,
) -> list[OpeningResultSummaryResponse]:
    contexts, ambiguous_keys = resolve_bid_notice_contexts(
        db,
        [(row.bid_notice_no, row.bid_notice_ord) for row in rows],
    )
    entries_by_round: dict[int, list[BidOpeningEntryModel]] = {}
    round_ids = [row.id for row in rows]
    if round_ids:
        entries = db.execute(
            select(BidOpeningEntryModel)
            .where(BidOpeningEntryModel.round_id.in_(round_ids))
            .order_by(BidOpeningEntryModel.round_id.asc(), BidOpeningEntryModel.id.asc())
        ).scalars()
        for entry in entries:
            entries_by_round.setdefault(entry.round_id, []).append(entry)

    responses: list[OpeningResultSummaryResponse] = []
    for row in rows:
        base = OpeningResultSummaryResponse.model_validate(row)
        ranked_entries = organize_entry_rankings(entries_by_round.get(row.id, []))
        first_rank_entry = (
            ranked_entries[0].entry
            if ranked_entries and ranked_entries[0].rank == 1
            else None
        )
        key = canonical_notice_key(row.bid_notice_no, row.bid_notice_ord)
        context = contexts.get(key)
        detail_pending = (
            row.status in {OpeningStatus.OPENED.value, OpeningStatus.AWARDED.value}
            and row.entries_collected_at is None
        )
        block_reasons: list[str] = []
        if detail_pending:
            block_reasons.append("entries_collected_at")
        if key in ambiguous_keys:
            block_reasons.append("ambiguous_bid_notice_context")
        elif context is None:
            block_reasons.append("bid_notice_context")
        else:
            block_reasons.extend(missing_bid_notice_context_fields(context))

        if detail_pending:
            export_status = "DETAIL_PENDING"
        elif key in ambiguous_keys:
            export_status = "NOTICE_CONTEXT_AMBIGUOUS"
        elif block_reasons:
            export_status = "NOTICE_CONTEXT_MISSING"
        else:
            export_status = "READY"

        responses.append(
            base.model_copy(
                update={
                    "business_name": context.business_name if context else None,
                    "demand_agency_name": (
                        context.demand_agency_name
                        if context and context.demand_agency_name
                        else base.demand_agency_name
                    ),
                    "base_amount": context.base_amount if context else None,
                    "prearranged_price_decision_method": (
                        context.prearranged_price_decision_method if context else None
                    ),
                    "proposal_deadline": context.proposal_deadline if context else None,
                    "region_restriction": context.region_restriction if context else None,
                    "region_restriction_api_status": (
                        context.region_restriction_api_status if context else None
                    ),
                    "is_two_stage_bid": context.is_two_stage_bid if context else None,
                    "sheet_export_status": export_status,
                    "sheet_exportable": not block_reasons,
                    "sheet_block_reasons": block_reasons,
                    "first_rank_company_name": (
                        first_rank_entry.company_name if first_rank_entry else None
                    ),
                    "first_rank_bid_price_score": (
                        first_rank_entry.bid_price_score if first_rank_entry else None
                    ),
                    "first_rank_technical_score": (
                        first_rank_entry.technical_score if first_rank_entry else None
                    ),
                }
            )
        )
    return responses


@router.post("/collect", response_model=CollectOpeningResultsResponse)
def collect_results(
    request: CollectOpeningResultsRequest,
    auth: dict = Depends(require_organization_auth),
    db: Session = Depends(get_db),
) -> CollectOpeningResultsResponse:
    if auth.get("role") != "admin":
        raise HTTPException(status_code=403, detail="시스템 관리자만 공통 원본 수집을 실행할 수 있습니다.")
    try:
        return collect_opening_results(db, request)
    except OpeningResultApiConfigurationError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except OpeningResultApiError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    except OpeningResultCollectionLeaseLostError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except OpeningResultCollectionConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post(
    "/internal/collect",
    response_model=ScheduledCollectOpeningResultsResponse,
)
def collect_results_on_schedule(
    _: None = Depends(verify_scraper_internal_token),
    __: None = Depends(verify_cloud_scheduler_oidc_token),
    db: Session = Depends(get_db),
) -> ScheduledCollectOpeningResultsResponse:
    try:
        response = run_scheduled_opening_results(db)
        if response.skipped_existing_run and response.run_status != "SUCCESS":
            raise HTTPException(
                status_code=409,
                detail="같은 정기 수집 슬롯의 작업이 아직 완료되지 않았습니다.",
            )
        return response
    except OpeningResultApiConfigurationError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except OpeningResultApiError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    except OpeningResultCollectionLeaseLostError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except OpeningResultCollectionConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.get("", response_model=OpeningResultListResponse)
def fetch_results(
    q: str | None = None,
    status: OpeningStatus | None = None,
    opened_from: datetime | None = None,
    opened_to: datetime | None = None,
    sheet_export_status: Literal[
        "READY",
        "DETAIL_PENDING",
        "NOTICE_CONTEXT_MISSING",
        "NOTICE_CONTEXT_AMBIGUOUS",
        "BLOCKED",
    ]
    | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=30, ge=1, le=100),
    auth: dict = Depends(require_organization_auth),
    db: Session = Depends(get_db),
) -> OpeningResultListResponse:
    query = OpeningResultListQuery(
        q=q,
        status=status,
        opened_from=opened_from,
        opened_to=opened_to,
        page=page,
        page_size=page_size,
    )
    rows, total = list_opening_results(
        db,
        query,
        organization_id=auth["organization_id"],
        user_id=auth["user_id"],
        paginate=sheet_export_status is None,
    )
    items = _summary_responses(db, rows)
    if sheet_export_status is not None:
        if sheet_export_status == "BLOCKED":
            items = [item for item in items if not item.sheet_exportable]
        else:
            items = [
                item
                for item in items
                if item.sheet_export_status == sheet_export_status
            ]
        total = len(items)
        start = (page - 1) * page_size
        items = items[start : start + page_size]
    return OpeningResultListResponse(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/settings", response_model=OpeningResultSettingsResponse)
def fetch_result_settings(
    auth: dict = Depends(require_organization_auth),
    db: Session = Depends(get_db),
) -> OpeningResultSettingsResponse:
    profile = get_user_result_profile(
        db,
        organization_id=auth["organization_id"],
        user_id=auth["user_id"],
    )
    destinations = list_sheet_destinations(
        db,
        organization_id=auth["organization_id"],
        user_id=auth["user_id"],
        include_organization=_can_manage_organization(auth),
    )
    return OpeningResultSettingsResponse(
        organization_id=auth["organization_id"],
        organization_name=auth["organization_name"],
        organization_role=auth["organization_role"],
        sheet_service_account_email=get_sheet_service_account_email(),
        profile=OpeningResultProfileResponse(
            enabled=profile.enabled,
            keywords=normalize_keywords(profile.keywords),
            excluded_keywords=normalize_keywords(profile.excluded_keywords),
        ),
        sheet_destinations=[_destination_response(item) for item in destinations],
    )


@router.put("/settings/profile", response_model=OpeningResultProfileResponse)
def save_result_profile(
    request: OpeningResultProfileUpdateRequest,
    auth: dict = Depends(require_organization_auth),
    db: Session = Depends(get_db),
) -> OpeningResultProfileResponse:
    profile = update_user_result_profile(
        db,
        organization_id=auth["organization_id"],
        user_id=auth["user_id"],
        enabled=request.enabled,
        keywords=request.keywords,
        excluded_keywords=request.excluded_keywords,
    )
    return OpeningResultProfileResponse(
        enabled=profile.enabled,
        keywords=normalize_keywords(profile.keywords),
        excluded_keywords=normalize_keywords(profile.excluded_keywords),
    )


@router.get("/sheet-destinations", response_model=list[SheetDestinationResponse])
def fetch_sheet_destinations(
    auth: dict = Depends(require_organization_auth),
    db: Session = Depends(get_db),
) -> list[SheetDestinationResponse]:
    rows = list_sheet_destinations(
        db,
        organization_id=auth["organization_id"],
        user_id=auth["user_id"],
        include_organization=_can_manage_organization(auth),
    )
    return [_destination_response(item) for item in rows]


@router.post(
    "/sheet-destinations/verify",
    response_model=SheetDestinationVerifyResponse,
)
def verify_sheet_destination(
    request: SheetDestinationVerifyRequest,
    auth: dict = Depends(require_organization_auth),
    db: Session = Depends(get_db),
) -> SheetDestinationVerifyResponse:
    try:
        spreadsheet_id = normalize_spreadsheet_id(request.spreadsheet_id)
        ensure_sheet_target_access(
            db,
            organization_id=auth["organization_id"],
            user_id=auth["user_id"],
            spreadsheet_id=spreadsheet_id,
            tab_name=request.tab_name,
            include_organization=_can_manage_organization(auth),
        )
        writer = GoogleSheetWriter.from_env(
            spreadsheet_id=spreadsheet_id,
            tab_name=request.tab_name,
        )
        verification = writer.verify_connection()
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except SheetDestinationAccessError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except SheetExportConfigurationError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(
            status_code=502,
            detail="Google Sheet 연결 확인에 실패했습니다. 서비스계정 공유 권한을 확인하세요.",
        ) from error
    return SheetDestinationVerifyResponse(
        spreadsheet_id=spreadsheet_id,
        spreadsheet_title=verification.spreadsheet_title,
        tab_name=request.tab_name,
        tab_exists=verification.tab_exists,
        header_status=verification.header_status,
        connection_ready=(
            verification.tab_exists
            and verification.header_status in {"MATCH", "EMPTY"}
        ),
        sheet_service_account_email=get_sheet_service_account_email(),
    )


@router.post("/sheet-destinations", response_model=SheetDestinationResponse)
def upsert_sheet_destination(
    request: SheetDestinationUpsertRequest,
    auth: dict = Depends(require_organization_auth),
    db: Session = Depends(get_db),
) -> SheetDestinationResponse:
    try:
        destination = save_sheet_destination(
            db,
            organization_id=auth["organization_id"],
            user_id=auth["user_id"],
            destination_id=request.destination_id,
            label=request.label,
            spreadsheet_id=request.spreadsheet_id,
            tab_name=request.tab_name,
            scope=request.scope,
            is_default=request.is_default,
            can_manage_organization=_can_manage_organization(auth),
        )
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except SheetDestinationAccessError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except SheetDestinationConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return _destination_response(destination)


@router.delete("/sheet-destinations/{destination_id}", status_code=204)
def delete_sheet_destination(
    destination_id: int,
    auth: dict = Depends(require_organization_auth),
    db: Session = Depends(get_db),
) -> None:
    try:
        deactivate_sheet_destination(
            db,
            organization_id=auth["organization_id"],
            user_id=auth["user_id"],
            destination_id=destination_id,
            can_manage_organization=_can_manage_organization(auth),
        )
    except SheetDestinationAccessError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.post("/export/sheet", response_model=ExportOpeningResultsSheetResponse)
def export_results_sheet(
    request: ExportOpeningResultsSheetRequest,
    auth: dict = Depends(require_organization_auth),
    db: Session = Depends(get_db),
) -> ExportOpeningResultsSheetResponse:
    can_manage_organization = _can_manage_organization(auth)
    try:
        destination = resolve_sheet_destination(
            db,
            organization_id=auth["organization_id"],
            user_id=auth["user_id"],
            destination_id=request.destination_id,
            include_organization=(
                can_manage_organization or request.destination_id is not None
            ),
        )
        if destination.owner_user_id is None and not can_manage_organization:
            raise PermissionError("조직 관리자만 조직 공용 Sheet에 반영할 수 있습니다.")
        selected_rounds = load_visible_results(
            db,
            organization_id=auth["organization_id"],
            user_id=auth["user_id"],
            result_ids=request.result_ids,
        )
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    except SheetDestinationAccessError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ResultAccessError as error:
        raise HTTPException(
            status_code=404,
            detail="선택한 개찰결과를 내 검토 목록에서 찾을 수 없습니다.",
        ) from error
    pending_detail_result_ids = [
        round_row.id
        for round_row in selected_rounds
        if round_row.status in {"OPENED", "AWARDED"}
        and round_row.entries_collected_at is None
    ]
    if pending_detail_result_ids:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "업체별 순위·점수 상세 수집이 끝나지 않아 Sheet에 반영할 수 없습니다.",
                "pending_detail_result_ids": pending_detail_result_ids,
            },
        )
    try:
        rows, missing_context_keys, missing_result_ids = build_sheet_rows(
            db,
            request.result_ids,
            selected_rounds=selected_rounds,
        )
    except AmbiguousBidNoticeContextError as error:
        raise HTTPException(
            status_code=409,
            detail={
                "message": str(error),
                "ambiguous_notice_context_keys": error.context_keys,
            },
        ) from error
    duplicate_notice_numbers = find_duplicate_notice_numbers(rows)
    if duplicate_notice_numbers:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "같은 공고번호의 개찰 회차는 한 번에 하나만 선택할 수 있습니다.",
                "duplicate_notice_numbers": duplicate_notice_numbers,
            },
        )
    preview_token = build_sheet_preview_token(
        destination_id=destination.id,
        spreadsheet_id=destination.spreadsheet_id,
        tab_name=destination.tab_name,
        result_ids=request.result_ids,
        rows=rows,
    )
    written = False
    inserted_count = 0
    updated_count = 0
    claim_batch = None
    if not request.dry_run:
        if missing_result_ids:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "선택한 개찰결과를 찾을 수 없습니다.",
                    "missing_result_ids": missing_result_ids,
                },
            )
        if not rows:
            raise HTTPException(
                status_code=409,
                detail="기록할 개찰결과가 없어 Sheet에 반영하지 않았습니다.",
            )
        if missing_context_keys:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "입찰공고 필수 정보가 누락되어 Sheet에 반영하지 않았습니다.",
                    "missing_notice_context_keys": missing_context_keys,
                },
            )
        if request.expected_preview_token is None:
            raise HTTPException(
                status_code=409,
                detail="먼저 미리보기를 확인한 뒤 Google Sheet 반영을 실행하세요.",
            )
        if not hmac.compare_digest(request.expected_preview_token, preview_token):
            raise HTTPException(
                status_code=409,
                detail="미리보기 이후 결과 또는 Sheet 목적지가 변경되었습니다. 다시 확인하세요.",
            )
        try:
            claim_batch = claim_sheet_exports(
                db,
                destination=destination,
                organization_id=auth["organization_id"],
                user_id=auth["user_id"],
                rounds=selected_rounds,
            )
            writer = GoogleSheetWriter.from_env(
                spreadsheet_id=destination.spreadsheet_id,
                tab_name=destination.tab_name,
            )
            upsert_result = writer.upsert(rows)
            inserted_count = upsert_result.inserted_count
            updated_count = upsert_result.updated_count
            complete_sheet_exports(
                db,
                claim_batch=claim_batch,
                organization_id=auth["organization_id"],
                user_id=auth["user_id"],
            )
            written = True
        except SheetExportConflictError as error:
            if claim_batch:
                fail_sheet_exports(
                    db,
                    claim_batch=claim_batch,
                    error_message=str(error),
                )
            raise HTTPException(status_code=409, detail=str(error)) from error
        except SheetExportConfigurationError as error:
            if claim_batch:
                fail_sheet_exports(
                    db,
                    claim_batch=claim_batch,
                    error_message=str(error),
                )
            raise HTTPException(status_code=503, detail=str(error)) from error
        except Exception as error:
            if claim_batch:
                fail_sheet_exports(
                    db,
                    claim_batch=claim_batch,
                    error_message=str(error),
                )
            raise HTTPException(status_code=502, detail="Google Sheet 기록에 실패했습니다.") from error
    return ExportOpeningResultsSheetResponse(
        headers=SHEET_HEADERS,
        requested_result_count=len(request.result_ids),
        row_count=len(rows),
        missing_result_ids=missing_result_ids,
        missing_notice_context_count=len(missing_context_keys),
        missing_notice_context_keys=missing_context_keys,
        written=written,
        inserted_count=inserted_count,
        updated_count=updated_count,
        preview_rows=rows,
        destination_id=destination.id,
        destination_label=destination.label,
        destination_scope="PERSONAL" if destination.owner_user_id is not None else "ORGANIZATION",
        destination_tab_name=destination.tab_name,
        preview_token=preview_token,
    )


@router.post("/{result_id}/restore", response_model=RestoreOpeningResultResponse)
def restore_result_to_inbox(
    result_id: int,
    auth: dict = Depends(require_organization_auth),
    db: Session = Depends(get_db),
) -> RestoreOpeningResultResponse:
    try:
        visible = restore_dismissed_result(
            db,
            organization_id=auth["organization_id"],
            user_id=auth["user_id"],
            result_id=result_id,
        )
    except ResultAccessError as error:
        raise HTTPException(
            status_code=404,
            detail="실행취소할 제외 상태를 찾을 수 없습니다.",
        ) from error
    return RestoreOpeningResultResponse(result_id=result_id, visible=visible)


@router.delete("/{result_id}", response_model=DismissOpeningResultResponse)
def delete_result_from_inbox(
    result_id: int,
    auth: dict = Depends(require_organization_auth),
    db: Session = Depends(get_db),
) -> DismissOpeningResultResponse:
    try:
        dismiss_result(
            db,
            organization_id=auth["organization_id"],
            user_id=auth["user_id"],
            result_id=result_id,
        )
    except ResultAccessError as error:
        raise HTTPException(status_code=404, detail="개찰결과를 찾을 수 없습니다.") from error
    return DismissOpeningResultResponse(result_id=result_id)


@router.get("/{result_id}", response_model=OpeningResultDetailResponse)
def fetch_result_detail(
    result_id: int,
    auth: dict = Depends(require_organization_auth),
    db: Session = Depends(get_db),
) -> OpeningResultDetailResponse:
    result = get_opening_result(
        db,
        result_id,
        organization_id=auth["organization_id"],
        user_id=auth["user_id"],
    )
    if result is None:
        raise HTTPException(status_code=404, detail="개찰결과를 찾을 수 없습니다.")
    round_row, entries = result
    summary = _summary_responses(db, [round_row])[0]
    calculated_ranks = {
        ranked.entry.id: ranked.rank
        for ranked in organize_entry_rankings(entries)
    }
    ordered_entries = sorted(
        entries,
        key=lambda entry: (
            calculated_ranks.get(entry.id) is None,
            calculated_ranks.get(entry.id) or 0,
            entry.id,
        ),
    )
    return OpeningResultDetailResponse(
        **summary.model_dump(),
        opening_notice=round_row.opening_notice,
        entries=[
            OpeningEntryResponse.model_validate(entry).model_copy(
                update={"rank": calculated_ranks.get(entry.id, entry.rank)}
            )
            for entry in ordered_entries
        ],
    )
