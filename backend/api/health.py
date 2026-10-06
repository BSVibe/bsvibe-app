"""Product-level health endpoint for ``/api/health``.

The lifted ``backend.shared.fastapi.health`` provides a generic primitive
(``make_health_router``). Phase 0 keeps this thin product route separate so the
contract (status / version / git_sha) is explicit.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel

from backend.auth.client import SupabaseAuthClient, get_supabase_client
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


class DependencyHealthResponse(BaseModel):
    status: str


@router.get("/health/auth", response_model=DependencyHealthResponse)
async def auth_health(
    supabase: Annotated[SupabaseAuthClient, Depends(get_supabase_client)],
    response: Response,
) -> DependencyHealthResponse:
    """Is the auth dependency (Supabase GoTrue) answering?

    The liveness probes read this instead of POSTing a bogus password to
    ``/api/auth/login``: that reading was a failed sign-in every minute — a
    ``supabase_token_failed`` warning in the log and a share of the per-IP
    sign-in rate limit every user's login goes through.
    """
    if await supabase.health():
        return DependencyHealthResponse(status="ok")
    response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return DependencyHealthResponse(status="unavailable")
