"""Async Supabase GoTrue client used by the ``/api/auth/*`` routes.

Only the four operations the auth routes need are implemented: password
login, OAuth (PKCE) code exchange, refresh, and logout. Each returns a
:class:`SupabaseSession` carrying the tokens plus the Supabase subject +
email lifted from the GoTrue ``user`` object — the bootstrap (§10.1) keys on
those without a second token decode.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

import httpx
import structlog
from pydantic import BaseModel, ConfigDict

from backend.config import get_settings

logger = structlog.get_logger(__name__)


class SupabaseAuthError(Exception):
    """Supabase rejected the request (bad credentials, invalid code, ...)."""


class SupabaseSession(BaseModel):
    """Tokens + identity returned by a successful GoTrue auth exchange."""

    model_config = ConfigDict(extra="ignore")

    access_token: str
    refresh_token: str
    expires_in: int = 3600
    token_type: str = "bearer"  # noqa: S105 — OAuth token_type label, not a secret
    supabase_user_id: str
    email: str | None = None


def _session_from_gotrue(body: dict[str, Any]) -> SupabaseSession:
    user = body.get("user") or {}
    sub = user.get("id")
    if not isinstance(sub, str) or not sub:
        raise SupabaseAuthError("Supabase response missing user id")
    return SupabaseSession(
        access_token=str(body.get("access_token", "")),
        refresh_token=str(body.get("refresh_token", "")),
        expires_in=int(body.get("expires_in", 3600)),
        token_type=str(body.get("token_type", "bearer")),
        supabase_user_id=sub,
        email=user.get("email"),
    )


class SupabaseAuthClient:
    """Thin wrapper over the GoTrue REST endpoints."""

    def __init__(
        self, *, base_url: str, publishable_key: str, http: httpx.AsyncClient | None = None
    ):
        self._base_url = base_url.rstrip("/")
        self._publishable_key = publishable_key
        self._http = http or httpx.AsyncClient(timeout=10.0)

    def _headers(self, access_token: str | None = None) -> dict[str, str]:
        headers = {"apikey": self._publishable_key, "Content-Type": "application/json"}
        headers["Authorization"] = f"Bearer {access_token or self._publishable_key}"
        return headers

    async def _token(self, grant_type: str, payload: dict[str, Any]) -> SupabaseSession:
        url = f"{self._base_url}/auth/v1/token"
        resp = await self._http.post(
            url, params={"grant_type": grant_type}, json=payload, headers=self._headers()
        )
        if resp.status_code >= 400:
            logger.warning("supabase_token_failed", grant_type=grant_type, status=resp.status_code)
            raise SupabaseAuthError(f"supabase {grant_type} failed ({resp.status_code})")
        return _session_from_gotrue(resp.json())

    async def password_login(self, email: str, password: str) -> SupabaseSession:
        return await self._token("password", {"email": email, "password": password})

    async def exchange_code_for_session(
        self, code: str, code_verifier: str | None = None
    ) -> SupabaseSession:
        payload: dict[str, Any] = {"auth_code": code}
        if code_verifier is not None:
            payload["code_verifier"] = code_verifier
        return await self._token("pkce", payload)

    async def refresh(self, refresh_token: str) -> SupabaseSession:
        return await self._token("refresh_token", {"refresh_token": refresh_token})

    async def logout(self, access_token: str) -> None:
        url = f"{self._base_url}/auth/v1/logout"
        resp = await self._http.post(url, headers=self._headers(access_token))
        # 204 = signed out; 401 = token already invalid — both are terminal.
        if resp.status_code not in (200, 204, 401):
            logger.warning("supabase_logout_failed", status=resp.status_code)
            raise SupabaseAuthError(f"supabase logout failed ({resp.status_code})")

    def build_authorize_url(self, provider: str, redirect_to: str, code_challenge: str) -> str:
        """Assemble the GoTrue ``/authorize`` URL for a social provider (PKCE).

        Pure string assembly — no HTTP. The browser is redirected here; GoTrue
        sends the user to the provider and back to ``redirect_to`` with a
        ``?code=`` the frontend exchanges via ``/api/auth/oauth/{provider}/callback``.
        The matching ``code_verifier`` is held client-side (sessionStorage).
        """
        query = urlencode(
            {
                "provider": provider,
                "redirect_to": redirect_to,
                "code_challenge": code_challenge,
                "code_challenge_method": "s256",
            }
        )
        return f"{self._base_url}/auth/v1/authorize?{query}"

    async def send_password_reset(self, email: str, redirect_to: str | None = None) -> None:
        """Ask GoTrue to email a recovery link (``/auth/v1/recover``).

        GoTrue returns 200 whether or not the email exists, so this never leaks
        account existence. A non-2xx is an infra error → ``SupabaseAuthError``;
        the route swallows it into a uniform 204 so the client learns nothing.
        """
        url = f"{self._base_url}/auth/v1/recover"
        payload: dict[str, Any] = {"email": email}
        if redirect_to is not None:
            payload["redirect_to"] = redirect_to
        resp = await self._http.post(url, json=payload, headers=self._headers())
        if resp.status_code >= 400:
            logger.warning("supabase_recover_failed", status=resp.status_code)
            raise SupabaseAuthError(f"supabase recover failed ({resp.status_code})")


_client_singleton: SupabaseAuthClient | None = None


def get_supabase_client() -> SupabaseAuthClient:
    """Process-wide Supabase client (lazy init). Tests override this dep."""
    global _client_singleton  # noqa: PLW0603 — module-level singleton intentional
    if _client_singleton is None:
        settings = get_settings()
        _client_singleton = SupabaseAuthClient(
            base_url=settings.supabase_url, publishable_key=settings.supabase_publishable_key
        )
    return _client_singleton
