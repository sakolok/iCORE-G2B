from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import settings
from app.data.bootstrap import ensure_schema_compatibility, seed_defaults
from app.data.database import SessionLocal, engine
from app.data.models import Base
from app.routers.auth import router as auth_router
from app.routers.health import router as health_router
from app.routers.scraper import router as scraper_router
from app.g2b.bid_notices.router import router as bid_notices_router
from app.g2b.opening_results.router import router as opening_results_router

app = FastAPI(title=settings.app_name, version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.cors_allowed_origins),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health_router)
app.include_router(auth_router)
app.include_router(scraper_router)
app.include_router(bid_notices_router)
app.include_router(opening_results_router)
if settings.g2b_pre_specifications_enabled:
    from app.g2b.pre_specifications.router import router as pre_specifications_router

    app.include_router(pre_specifications_router)


@app.on_event("startup")
def startup() -> None:
    Base.metadata.create_all(bind=engine)
    ensure_schema_compatibility(engine)
    with SessionLocal() as db:
        seed_defaults(db)

@app.get("/")
def read_root():
    return {"status": "ok", "message": "Icore API is running"}
