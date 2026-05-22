"""FastAPI application factory.

Entrypoint:
    uvicorn backend.api.main:create_app --factory --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

from fastapi import FastAPI

from backend.api.health import router as health_router
from backend.api.v1 import router as v1_router
from backend.config import get_settings
from backend.shared.core.logging import configure_logging


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(level="INFO", service_name="bsvibe-app")

    app = FastAPI(
        title="BSVibe",
        version=settings.version,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )
    app.include_router(health_router, prefix="/api")
    app.include_router(v1_router, prefix="/api")
    return app
