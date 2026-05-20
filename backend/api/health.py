"""Product-level health endpoint for ``/api/health``.

The lifted ``backend.shared.fastapi.health`` provides a generic primitive
(``make_health_router``). Phase 0 keeps this thin product route separate so the
contract (status / version / git_sha) is explicit.
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from backend.config import Settings, get_settings

router = APIRouter()


class HealthResponse(BaseModel):
    status: str
    version: str
    git_sha: str


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    settings: Settings = get_settings()
    return HealthResponse(
        status="ok",
        version=settings.version,
        git_sha=settings.git_sha,
    )
