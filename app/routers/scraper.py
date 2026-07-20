from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.data.database import get_db
from app.schemas import (
    ScraperConfig,
    ScraperDedupFilterRequest,
    ScraperDedupFilterResponse,
    ScraperRunReportRequest,
    ScraperRunReportResponse,
    ScraperRunSummary,
    TriggerScraperRequest,
    TriggerScraperResponse,
)
from app.services.auth_service import require_auth, verify_scraper_internal_token
from app.g2b.bid_notices.service import (
    create_scraper_task,
    filter_new_scraper_notices,
    get_last_scraper_run_time,
    get_scraper_config,
    list_scraper_runs,
    record_scraper_run_report,
    run_scraper_pipeline,
    upsert_scraper_config,
)
from app.services.cloud_scheduler_service import sync_scheduler_job

router = APIRouter(prefix="/api/scraper", tags=["scraper"])


@router.get("/config", response_model=ScraperConfig)
def fetch_scraper_config(
    _: dict = Depends(require_auth), db: Session = Depends(get_db)
) -> ScraperConfig:
    return get_scraper_config(db)


@router.put("/config")
def update_scraper_config(
    config: ScraperConfig,
    _: dict = Depends(require_auth),
    db: Session = Depends(get_db),
) -> dict:
    saved_config = upsert_scraper_config(db, config)
    scheduler_status = sync_scheduler_job(saved_config)
    return {
        "success": True,
        "message": "Scraper 설정이 저장되었습니다.",
        "config": saved_config.model_dump(mode="json"),
        "scheduler": scheduler_status.model_dump(mode="json"),
    }


@router.post("/trigger", response_model=TriggerScraperResponse)
def trigger_scraper(
    request: TriggerScraperRequest,
    _: dict = Depends(require_auth),
    db: Session = Depends(get_db),
) -> TriggerScraperResponse:
    config = get_scraper_config(db)
    return create_scraper_task(config=config, reason=request.reason)


@router.get("/runs", response_model=list[ScraperRunSummary])
def fetch_scraper_runs(
    limit: int = 20,
    _: dict = Depends(require_auth),
    db: Session = Depends(get_db),
) -> list[ScraperRunSummary]:
    return list_scraper_runs(db, limit=limit)


@router.get("/internal/last-run")
def fetch_last_run_time(
    _: None = Depends(verify_scraper_internal_token),
    db: Session = Depends(get_db),
) -> dict:
    last_run_at = get_last_scraper_run_time(db)
    return {"last_run_at": last_run_at.isoformat() if last_run_at else None}


@router.post("/internal/dedup", response_model=ScraperDedupFilterResponse)
def dedup_notices(
    payload: ScraperDedupFilterRequest,
    _: None = Depends(verify_scraper_internal_token),
    db: Session = Depends(get_db),
) -> ScraperDedupFilterResponse:
    return filter_new_scraper_notices(db, payload)


@router.post("/runs", response_model=ScraperRunReportResponse)
def report_scraper_run(
    payload: ScraperRunReportRequest,
    _: None = Depends(verify_scraper_internal_token),
    db: Session = Depends(get_db),
) -> ScraperRunReportResponse:
    return record_scraper_run_report(db, payload)


@router.post("/execute", response_model=TriggerScraperResponse)
def execute_scraper(
    request: TriggerScraperRequest,
    _: dict = Depends(require_auth),
    db: Session = Depends(get_db),
) -> TriggerScraperResponse:
    config = get_scraper_config(db)
    return run_scraper_pipeline(db=db, config=config, reason=request.reason)
