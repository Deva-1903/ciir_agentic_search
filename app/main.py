"""
FastAPI application entry point.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.requests import Request
from fastapi.responses import HTMLResponse

from app.api.routes_export import router as export_router
from app.api.routes_search import router as search_router
from app.core.config import get_settings
from app.core.logging import get_logger, setup_logging
from app.models.db import init_db

setup_logging()
log = get_logger(__name__)

BASE_DIR = Path(__file__).parent.parent


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    log.info("Starting AgenticSearch  env=%s  llm=%s  model=%s",
             settings.app_env, settings.llm_provider, settings.active_model)
    await init_db()
    yield
    log.info("Shutting down")


app = FastAPI(
    title="AgenticSearch",
    description="Provenance-first entity discovery via multi-angle web search",
    version="0.1.0",
    lifespan=lifespan,
)

# ── Static files and templates ────────────────────────────────────────────────

app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")

# ── API routers ───────────────────────────────────────────────────────────────

app.include_router(search_router, prefix="/api")
app.include_router(export_router, prefix="/api")


# ── UI routes ─────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request=request, name="index.html")
