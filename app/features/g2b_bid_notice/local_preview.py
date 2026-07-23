"""Local-only API runner for the isolated G2B bid-notice search feature."""

import logging
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.features.g2b_bid_notice.schemas import (
    BidNoticePreviewRequest,
    BidNoticePreviewResponse,
    SelectedBidNoticesSaveRequest,
    SelectedBidNoticesSaveResponse,
)
from app.features.g2b_bid_notice.service import preview_bid_notices
from app.features.g2b_bid_notice.sheets import (
    SheetsIntegrationError,
    append_connection_test_row,
    append_selected_bid_notices,
)


# app/features/g2b_bid_notice/local_preview.py -> icore-backend/.env
ENV_PATH = Path(__file__).resolve().parents[3] / ".env"
load_dotenv(ENV_PATH)
logger = logging.getLogger(__name__)

app = FastAPI(title="iCore G2B Bid Notice Local Preview", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:5174", "http://localhost:5174"],
    allow_credentials=False,
    allow_methods=["POST"],
    allow_headers=["Content-Type"],
)


@app.exception_handler(RequestValidationError)
async def request_validation_error_handler(
    _: Request,
    error: RequestValidationError,
) -> JSONResponse:
    """Return a concise, actionable validation error to the local preview."""

    fields = [
        ".".join(str(part) for part in issue.get("loc", ()) if part != "body")
        for issue in error.errors()
    ]
    field_summary = ", ".join(field for field in fields if field) or "요청 값"
    logger.warning("Local preview request validation failed for: %s", field_summary)
    return JSONResponse(
        status_code=422,
        content={"detail": f"입력 값 형식이 맞지 않습니다: {field_summary}"},
    )


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/bid-notice-search/preview", response_model=BidNoticePreviewResponse)
def preview(payload: BidNoticePreviewRequest) -> BidNoticePreviewResponse:
    try:
        # Re-read local settings so changing .env does not require a server restart.
        load_dotenv(ENV_PATH, override=True)
        return preview_bid_notices(payload)
    except ValueError as error:
        cause = error.__cause__
        response = getattr(cause, "response", None)
        logger.warning(
            "Local preview query was rejected: message=%s cause=%s http_status=%s",
            error,
            type(cause).__name__ if cause else "none",
            getattr(response, "status_code", None),
        )
        raise HTTPException(status_code=422, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=502, detail="나라장터 입찰공고 조회에 실패했습니다.") from error


@app.post("/api/bid-notice-search/sheets/connection-test")
def sheets_connection_test() -> dict[str, str]:
    try:
        load_dotenv(ENV_PATH, override=True)
        updated_range = append_connection_test_row()
        return {"status": "ok", "updated_range": updated_range}
    except SheetsIntegrationError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@app.post("/api/bid-notice-search/sheets/selected", response_model=SelectedBidNoticesSaveResponse)
def save_selected_notices(payload: SelectedBidNoticesSaveRequest) -> SelectedBidNoticesSaveResponse:
    """Persist only rows that the user explicitly checked in the preview."""

    try:
        load_dotenv(ENV_PATH, override=True)
        saved_count, skipped_duplicate_count, updated_range = append_selected_bid_notices(
            payload.selected_items
        )
        return SelectedBidNoticesSaveResponse(
            saved_count=saved_count,
            skipped_duplicate_count=skipped_duplicate_count,
            updated_range=updated_range,
        )
    except SheetsIntegrationError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
