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
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import get_db_session
from backend.auth.client import (
    SignUpResult,
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


class OAuthAuthorizeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # PKCE challenge derived client-side; the matching verifier stays in the
    # browser (sessionStorage) and is sent to .../callback for the exchange.
    code_challenge: str = Field(min_length=1)
    redirect_to: str = Field(min_length=1)


class OAuthAuthorizeResponse(BaseModel):
    authorize_url: str


class PasswordResetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=320)
    redirect_to: str | None = None


class SignUpRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=320)
    # GoTrue enforces the project's own minimum; this only rejects the empty
    # string so an obviously-blank submit never reaches the IdP.
    password: str = Field(min_length=1)
    redirect_to: str | None = None


class SignUpResponse(BaseModel):
    """Either a live session, or "check your mail" — never both.

    ``session`` is ``None`` exactly when ``confirmation_required`` is true. The
    client must branch on it: signing the user in on a pending confirmation
    would hand out a workspace before the address is proven.
    """

    model_config = ConfigDict(extra="forbid")

    session: SupabaseSession | None = None
    confirmation_required: bool = False


# Social providers the workspace supports (Supabase must have each configured).
_OAUTH_PROVIDERS = frozenset({"google", "github"})


def _validate_redirect_to(redirect_to: str) -> None:
    """Reject open redirects — the target origin must be CORS-allow-listed.

    Reuses ``cors_allowed_origins`` (the browser PWA's own origins) as the
    redirect allow-list so there is a single source of truth and no new knob.
    """
    parts = urlsplit(redirect_to)
    origin = f"{parts.scheme}://{parts.netloc}"
    if origin not in set(get_settings().cors_allowed_origins):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="redirect_to not allowed"
        )


async def _bootstrap(session: AsyncSession, supa: SupabaseSession) -> None:
    await ensure_user_bootstrapped(
        session,
        supabase_user_id=supa.supabase_user_id,
        email=supa.email,
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


@router.post("/oauth/{provider}/authorize")
async def oauth_authorize(
    provider: str, payload: OAuthAuthorizeRequest, supabase: SupabaseDep
) -> OAuthAuthorizeResponse:
    """Start social sign-in: return the GoTrue authorize URL to redirect to.

    Keeps the Supabase URL server-side. ``provider`` and ``redirect_to`` are
    both validated before any URL is assembled (no open redirect, no arbitrary
    provider).
    """
    if provider not in _OAUTH_PROVIDERS:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="unsupported provider")
    _validate_redirect_to(payload.redirect_to)
    url = supabase.build_authorize_url(provider, payload.redirect_to, payload.code_challenge)
    return OAuthAuthorizeResponse(authorize_url=url)


@router.post("/password/reset", status_code=status.HTTP_204_NO_CONTENT)
async def password_reset(payload: PasswordResetRequest, supabase: SupabaseDep) -> None:
    """Request a password-recovery email. Always 204 — never leaks existence."""
    if payload.redirect_to is not None:
        _validate_redirect_to(payload.redirect_to)
    try:
        await supabase.send_password_reset(payload.email, payload.redirect_to)
    except SupabaseAuthError:
        # Swallow: the caller must not learn whether the email exists or whether
        # GoTrue rejected it. The recovery email path is best-effort.
        pass


@router.post("/signup")
async def signup(
    payload: SignUpRequest, supabase: SupabaseDep, session: SessionDep
) -> SignUpResponse:
    """Create an account with email + password.

    ⚠️ This ships INERT: the Supabase project currently disables email signups,
    so GoTrue answers *"Signups not allowed for this instance"* and this returns
    403. That is the honest shape — a closed door, not a server fault. Opening it
    is a console setting, deliberately outside this repo.

    The user row is bootstrapped ONLY when a session comes back. On a pending
    confirmation the address is not yet proven, and creating the row then would
    let anyone claim a workspace under someone else's email.
    """
    if payload.redirect_to is not None:
        _validate_redirect_to(payload.redirect_to)
    try:
        result: SignUpResult = await supabase.sign_up(
            payload.email, payload.password, payload.redirect_to
        )
    except SupabaseAuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="signup is not available for this instance",
        ) from exc
    if result.session is not None:
        await _bootstrap(session, result.session)
    return SignUpResponse(
        session=result.session, confirmation_required=result.confirmation_required
    )


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
