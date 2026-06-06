"""Sentry integration provider — install→grant token mechanics (Lift 5).

Sentry is the design's odd one out (§3.0). It is NOT a standard
authorization_code redirect: the founder INSTALLS the Sentry integration, and
Sentry redirects back with an ``installation_id`` + a one-time grant ``code``.
The token is then minted at an INSTALLATION-scoped endpoint:

    POST /api/0/sentry-app-installations/{installation_id}/authorizations/

with ``grant_type=authorization_code`` (then ``refresh_token`` to rotate). The
response is camelCase (``token`` / ``refreshToken`` / ``expiresAt``), tokens are
~8h, and the installation token acts WITHOUT a user (service token).

Because exchange + refresh both need the ``installation_id`` (which the generic
:class:`backend.connectors.auth.oauth2.OAuth2Provider` and the generic
``/oauth/{provider}/{start,callback}`` flow do NOT carry), this is its own
class. Its ``exchange_code`` / ``refresh`` (the OAuthProvider Protocol shape)
raise with a pointer to the installation methods — a Sentry-specific connect
endpoint (capturing ``installation_id``) is the remaining wiring and is
deliberately separate from this provider's token mechanics.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx

from backend.connectors.auth.tokenset import TokenSet

_API_BASE = "https://sentry.io/api/0"
_HTTP_TIMEOUT = 10.0


class SentryProvider:
    """Mint + rotate a Sentry integration installation token."""

    name = "sentry"
    token_exchange_auth = "body"  # noqa: S105 — auth-style label, not a secret
    refreshable = True
    supports_service_token = True

    def __init__(self, *, client_id: str, client_secret: str) -> None:
        self._client_id = client_id
        self._client_secret = client_secret

    async def exchange_installation(self, *, installation_id: str, code: str) -> TokenSet:
        """Exchange the install grant ``code`` for the installation token."""
        return await self._authorize(
            installation_id,
            {"grant_type": "authorization_code", "code": code},
        )

    async def refresh_installation(self, *, installation_id: str, refresh_token: str) -> TokenSet:
        """Rotate an installation token via its refresh token."""
        return await self._authorize(
            installation_id,
            {"grant_type": "refresh_token", "refresh_token": refresh_token},
        )

    async def _authorize(self, installation_id: str, grant: dict[str, str]) -> TokenSet:
        body = {
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            **grant,
        }
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.post(
                f"{_API_BASE}/sentry-app-installations/{installation_id}/authorizations/",
                json=body,
                headers={"Accept": "application/json"},
            )
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()
        return TokenSet(
            access_token=data["token"],
            refresh_token=data.get("refreshToken"),
            expires_at=_parse_iso8601(data.get("expiresAt")),
            scopes=tuple(data.get("scopes", ())),
            account_label=None,
        )

    # ── OAuthProvider Protocol shape (Sentry doesn't fit the generic flow) ──

    def authorize_url(self, *, state: str, code_challenge: str, redirect_uri: str) -> str:
        raise NotImplementedError("sentry uses install→grant, not a standard authorize redirect")

    async def exchange_code(self, *, code: str, code_verifier: str, redirect_uri: str) -> TokenSet:
        raise NotImplementedError("sentry: use exchange_installation(installation_id, code)")

    async def refresh(self, *, refresh_token: str) -> TokenSet:
        raise NotImplementedError(
            "sentry: use refresh_installation(installation_id, refresh_token)"
        )

    async def service_token(self, *, install_ref: str) -> TokenSet:
        raise NotImplementedError("sentry: the installation token IS the service token")


def _parse_iso8601(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


__all__ = ["SentryProvider"]
