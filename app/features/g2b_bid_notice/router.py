from fastapi import APIRouter, Depends, HTTPException

from app.features.g2b_bid_notice.schemas import BidNoticePreviewRequest, BidNoticePreviewResponse
from app.features.g2b_bid_notice.service import preview_bid_notices
from app.services.auth_service import require_auth

router = APIRouter(prefix="/api/bid-notice-search", tags=["g2b-bid-notice-search"])


@router.post("/preview", response_model=BidNoticePreviewResponse)
def preview(
    payload: BidNoticePreviewRequest,
    _: dict = Depends(require_auth),
) -> BidNoticePreviewResponse:
    try:
        return preview_bid_notices(payload)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=502, detail="입찰공고 조회에 실패했습니다.") from error
