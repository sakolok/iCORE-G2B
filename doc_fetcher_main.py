from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from app.g2b.bid_notices.document_analysis import (
    AttachmentDownloadError,
    _download_attachment,
)


app = FastAPI(title="iCORE G2B Document Fetcher", version="0.1.0")


class FetchDocumentRequest(BaseModel):
    url: str


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/fetch")
def fetch_document(payload: FetchDocumentRequest) -> Response:
    try:
        content, content_type = _download_attachment(payload.url)
    except AttachmentDownloadError as error:
        status_code = 504 if error.retryable else 422
        raise HTTPException(status_code=status_code, detail=error.code) from error

    return Response(
        content=content,
        media_type=content_type or "application/octet-stream",
        headers={"X-Content-Type-Options": "nosniff"},
    )
