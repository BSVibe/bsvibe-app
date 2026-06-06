"""GitHubAppProvider — bsvibe as an OAuth *client* of GitHub (Lift 1).

The first real :class:`backend.connectors.auth.providers.OAuthProvider`
behind the Lift 0 skeleton. GitHub is the design's flagship connector and
exercises every knob:

* ``token_exchange_auth = "body"`` — the token endpoint reads the client
  credentials from the POST body (GitHub does NOT accept HTTP Basic here).
* ``refreshable = True`` — when the GitHub App opts into expiring user tokens,
  ``exchange_code`` returns a ``refresh_token`` good for 6 months.
* ``supports_service_token`` — True only when App credentials (``app_id`` +
  ``private_key_pem``) are configured. The installation access-token flow signs
  a short-lived App JWT (RS256) and mints a token that acts WITHOUT a user.

GitHub user-to-server authorization does NOT implement PKCE, so the skeleton's
``code_challenge`` is accepted-and-ignored here; the single-use ``state`` row
still provides CSRF protection end-to-end.

All HTTP goes through a plain ``httpx.AsyncClient`` (respx-mockable in tests).
No credential value is ever logged.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt

from backend.connectors.auth.providers import TokenExchangeAuth
from backend.connectors.auth.tokenset import TokenSet

_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
_TOKEN_URL = "https://github.com/login/oauth/access_token"  # noqa: S105 — endpoint URL, not a secret
_API_BASE = "https://api.github.com"

# App JWT lifetime — GitHub rejects anything over 10 minutes. Backdate ``iat``
# 60s to tolerate minor clock skew between bsvibe and GitHub.
_APP_JWT_TTL = 600
_APP_JWT_SKEW = 60
_HTTP_TIMEOUT = 10.0


class GitHubAppProvider:
    """Acquire + refresh GitHub credentials for the github connector."""

    name = "github"
    token_exchange_auth: TokenExchangeAuth = "body"  # noqa: S105 — auth-style label
    refreshable = True

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        app_id: str = "",
        private_key_pem: str = "",
        scopes: tuple[str, ...] = (),
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._app_id = app_id
        self._private_key_pem = private_key_pem
        self._scopes = scopes
        # Installation tokens need App credentials (app_id + private key); the
        # capability is off without them. Plain instance attribute (not a
        # property) so it satisfies the settable OAuthProvider Protocol member.
        self.supports_service_token = bool(app_id and private_key_pem)

    def authorize_url(self, *, state: str, code_challenge: str, redirect_uri: str) -> str:
        # GitHub does not support PKCE; ``code_challenge`` is intentionally
        # unused. ``state`` is the CSRF binding (single-use, server-stored).
        params: dict[str, str] = {
            "client_id": self._client_id,
            "redirect_uri": redirect_uri,
            "state": state,
        }
        if self._scopes:
            params["scope"] = " ".join(self._scopes)
        return f"{_AUTHORIZE_URL}?{urlencode(params)}"

    async def exchange_code(self, *, code: str, code_verifier: str, redirect_uri: str) -> TokenSet:
        # ``code_verifier`` unused (no PKCE on GitHub).
        payload = await self._token_request(
            {
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "code": code,
                "redirect_uri": redirect_uri,
            }
        )
        label = await self._fetch_account_label(payload["access_token"])
        return self._tokenset_from_payload(payload, account_label=label)

    async def refresh(self, *, refresh_token: str) -> TokenSet:
        payload = await self._token_request(
            {
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            }
        )
        return self._tokenset_from_payload(payload)

    async def service_token(self, *, install_ref: str) -> TokenSet:
        if not self.supports_service_token:
            raise NotImplementedError("github service tokens require app_id + private_key_pem")
        app_jwt = self._build_app_jwt()
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.post(
                f"{_API_BASE}/app/installations/{install_ref}/access_tokens",
                headers={
                    "Authorization": f"Bearer {app_jwt}",
                    "Accept": "application/vnd.github+json",
                },
            )
        resp.raise_for_status()
        data = resp.json()
        return TokenSet(
            access_token=data["token"],
            refresh_token=None,
            expires_at=_parse_iso8601(data.get("expires_at")),
            scopes=(),
            account_label=None,
        )

    # ── internals ──

    async def _token_request(self, data: dict[str, str]) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.post(
                _TOKEN_URL,
                data=data,
                headers={"Accept": "application/json"},
            )
        resp.raise_for_status()
        payload: dict[str, Any] = resp.json()
        # GitHub returns 200 with an ``error`` body on bad/expired codes.
        if "error" in payload:
            raise ValueError(
                f"github token exchange failed: {payload['error']} "
                f"({payload.get('error_description', '')})"
            )
        return payload

    async def _fetch_account_label(self, access_token: str) -> str | None:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.get(
                f"{_API_BASE}/user",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/vnd.github+json",
                },
            )
        if resp.status_code != httpx.codes.OK:
            return None
        login = resp.json().get("login")
        return f"@{login}" if login else None

    def _tokenset_from_payload(
        self, payload: dict[str, Any], *, account_label: str | None = None
    ) -> TokenSet:
        expires_in = payload.get("expires_in")
        expires_at = (
            datetime.now(tz=UTC) + timedelta(seconds=int(expires_in)) if expires_in else None
        )
        scope = payload.get("scope") or ""
        scopes = tuple(s for s in scope.split(",") if s)
        return TokenSet(
            access_token=payload["access_token"],
            refresh_token=payload.get("refresh_token"),
            expires_at=expires_at,
            scopes=scopes,
            account_label=account_label,
        )

    def _build_app_jwt(self) -> str:
        now = int(time.time())
        claims = {
            "iat": now - _APP_JWT_SKEW,
            "exp": now + _APP_JWT_TTL,
            "iss": self._app_id,
        }
        return jwt.encode(claims, self._private_key_pem, algorithm="RS256")


def _parse_iso8601(value: str | None) -> datetime | None:
    if not value:
        return None
    # GitHub uses ``2099-01-01T00:00:00Z``; normalise the trailing Z.
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


__all__ = ["GitHubAppProvider"]
