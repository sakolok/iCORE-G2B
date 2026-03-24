from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import settings
from app.routers.builder import router as builder_router
from app.routers.health import router as health_router
from app.routers.scraper import router as scraper_router
from app.routers.site_manager import router as site_router

app = FastAPI(title=settings.app_name, version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health_router)
app.include_router(builder_router)
app.include_router(site_router)
app.include_router(scraper_router)
