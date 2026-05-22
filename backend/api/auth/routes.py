"""/api/auth/* — login, OAuth callback, refresh, logout (Workflow §2.1).

The backend talks to Supabase GoTrue directly via
:class:`backend.auth.client.SupabaseAuthClient`. On a successful login or
OAuth code exchange the principal is bootstrapped (§10.1): first login upserts
the ``User`` and creates a personal ``Workspace`` + ``Membership(owner)`` when
the user has none. Token *verification* on subsequent requests is handled by
``backend.shared.authz`` via the v1 routers' auth dependency.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import get_db_session
from backend.auth.client import (
    SupabaseAuthClient,
    SupabaseAuthError,
    SupabaseSession,
    get_supabase_client,
)
from backend.config import get_settings
from backend.identity.service import ensure_user_bootstrapped

router = APIRouter(prefix="/auth", tags=["auth"])

SupabaseDep = Annotated[SupabaseAuthClient, Depends(get_supabase_client)]
SessionDep = Annotated[AsyncSession, Depends(get_db_session)]


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1)


class OAuthCallbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1)
    code_verifier: str | None = None


class RefreshRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    refresh_token: str = Field(min_length=1)


async def _bootstrap(session: AsyncSession, supa: SupabaseSession) -> None:
    settings = get_settings()
    await ensure_user_bootstrapped(
        session,
        supabase_user_id=supa.supabase_user_id,
        email=supa.email,
        region=settings.default_workspace_region,
    )


@router.post("/login")
async def login(
    payload: LoginRequest, supabase: SupabaseDep, session: SessionDep
) -> SupabaseSession:
    try:
        supa = await supabase.password_login(payload.email, payload.password)
    except SupabaseAuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid credentials"
        ) from exc
    await _bootstrap(session, supa)
    return supa


@router.post("/oauth/{provider}/callback")
async def oauth_callback(
    provider: str,
    payload: OAuthCallbackRequest,
    supabase: SupabaseDep,
    session: SessionDep,
) -> SupabaseSession:
    del provider  # routing-only; Supabase resolves the provider from the code
    try:
        supa = await supabase.exchange_code_for_session(payload.code, payload.code_verifier)
    except SupabaseAuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid authorization code"
        ) from exc
    await _bootstrap(session, supa)
    return supa


@router.post("/refresh")
async def refresh(payload: RefreshRequest, supabase: SupabaseDep) -> SupabaseSession:
    try:
        return await supabase.refresh(payload.refresh_token)
    except SupabaseAuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid refresh token"
        ) from exc


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    supabase: SupabaseDep,
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing bearer token")
    access_token = authorization.split(" ", 1)[1]
    try:
        await supabase.logout(access_token)
    except SupabaseAuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="logout failed"
        ) from exc
