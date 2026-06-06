"""OAuth2Provider — generic ``authorization_code`` provider (Lift 2 foundation).

slack / discord / notion share the vanilla authorization_code grant. Per design
§3 the only variation is three knobs — token-endpoint client auth (``body`` vs
``basic``), ``refreshable``, PKCE support — plus where the human-facing account
label comes from (a field in the token response, or a separate userinfo call).
This one data-driven class covers all three; each connector is just a
configured instance (see ``slack.py`` / ``notion.py`` / ``discord.py``).

GitHubAppProvider stays separate: GitHub adds App-JWT installation tokens and
does NOT support PKCE, so it has its own class rather than bending this one.
Sentry also stays separate (install→grant + service token).

All HTTP goes through a plain ``httpx.AsyncClient`` (respx-mockable). No
credential value is ever logged.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx

from backend.connectors.auth.providers import TokenExchangeAuth
from backend.connectors.auth.tokenset import TokenSet

_HTTP_TIMEOUT = 10.0
# Providers separate scopes with spaces (slack/discord) or commas (github);
# split on either so the stored scope list is normalised.
_SCOPE_SPLIT = re.compile(r"[ ,]+")


@dataclass
class OAuth2Provider:
    """A configured vanilla authorization_code OAuth provider."""

    name: str
    authorize_endpoint: str
    token_endpoint: str
    token_exchange_auth: TokenExchangeAuth
    refreshable: bool
    supports_pkce: bool
    scopes: tuple[str, ...]
    client_id: str
    client_secret: str
    # Where the account label lives. When ``userinfo_endpoint`` is set we GET it
    # with the access token and read ``label_path`` from THAT response;
    # otherwise ``label_path`` is read from the token response itself. Path is a
    # tuple of nested keys, e.g. ("team", "name").
    label_path: tuple[str, ...] | None = None
    userinfo_endpoint: str | None = None
    # Extra static query params on the authorize URL (e.g. Notion's
    # ``owner=user``). Merged after the standard params.
    extra_authorize_params: dict[str, str] = field(default_factory=dict)
    supports_service_token: bool = field(default=False)

    def authorize_url(self, *, state: str, code_challenge: str, redirect_uri: str) -> str:
        params: dict[str, str] = {
            "client_id": self.client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "state": state,
        }
        if self.scopes:
            params["scope"] = " ".join(self.scopes)
        if self.supports_pkce:
            params["code_challenge"] = code_challenge
            params["code_challenge_method"] = "S256"
        params.update(self.extra_authorize_params)
        return f"{self.authorize_endpoint}?{urlencode(params)}"

    async def exchange_code(self, *, code: str, code_verifier: str, redirect_uri: str) -> TokenSet:
        data: dict[str, str] = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
        }
        if self.supports_pkce:
            data["code_verifier"] = code_verifier
        payload = await self._token_request(data)
        label = await self._resolve_label(payload)
        return self._tokenset(payload, account_label=label)

    async def refresh(self, *, refresh_token: str) -> TokenSet:
        payload = await self._token_request(
            {"grant_type": "refresh_token", "refresh_token": refresh_token}
        )
        return self._tokenset(payload)

    async def service_token(self, *, install_ref: str) -> TokenSet:
        raise NotImplementedError(f"provider {self.name!r} does not support service tokens")

    # ── internals ──

    async def _token_request(self, data: dict[str, str]) -> dict[str, Any]:
        headers = {"Accept": "application/json"}
        body = dict(data)
        if self.token_exchange_auth == "basic":  # noqa: S105 — auth-style label, not a secret
            creds = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode(
                "ascii"
            )
            headers["Authorization"] = f"Basic {creds}"
        else:  # body
            body["client_id"] = self.client_id
            body["client_secret"] = self.client_secret

        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.post(self.token_endpoint, data=body, headers=headers)
        resp.raise_for_status()
        payload: dict[str, Any] = resp.json()
        # Slack returns ``{"ok": false, "error": ...}``; others a top-level
        # ``error``. Treat either as a failed exchange.
        if payload.get("error") or payload.get("ok") is False:
            raise ValueError(
                f"{self.name} token exchange failed: "
                f"{payload.get('error') or payload.get('error_description') or 'unknown'}"
            )
        return payload

    async def _resolve_label(self, token_payload: dict[str, Any]) -> str | None:
        if self.label_path is None:
            return None
        source = token_payload
        if self.userinfo_endpoint is not None:
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
                resp = await client.get(
                    self.userinfo_endpoint,
                    headers={
                        "Authorization": f"Bearer {token_payload['access_token']}",
                        "Accept": "application/json",
                    },
                )
            if resp.status_code != httpx.codes.OK:
                return None
            source = resp.json()
        return _dig(source, self.label_path)

    def _tokenset(self, payload: dict[str, Any], *, account_label: str | None = None) -> TokenSet:
        expires_in = payload.get("expires_in")
        expires_at = (
            datetime.now(tz=UTC) + timedelta(seconds=int(expires_in)) if expires_in else None
        )
        scope = payload.get("scope") or ""
        scopes = tuple(s for s in _SCOPE_SPLIT.split(scope) if s)
        return TokenSet(
            access_token=payload["access_token"],
            refresh_token=payload.get("refresh_token") if self.refreshable else None,
            expires_at=expires_at,
            scopes=scopes,
            account_label=account_label,
        )


def _dig(obj: dict[str, Any], path: tuple[str, ...]) -> str | None:
    """Read a nested key path; return its str value or None if absent."""
    cur: Any = obj
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return None
        cur = cur[key]
    return str(cur) if cur is not None else None


__all__ = ["OAuth2Provider"]
