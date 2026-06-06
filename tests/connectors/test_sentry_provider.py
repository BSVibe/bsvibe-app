"""SentryProvider — install→grant token mechanics (Lift 5).

Sentry mints an installation token at an installation-scoped endpoint, with a
camelCase response. These tests pin that exchange + refresh shape (respx-mocked).
The generic-flow methods (authorize_url / exchange_code / refresh) intentionally
raise — Sentry's connect endpoint (which captures the installation_id) is wired
separately, not through the generic /oauth/{provider} flow.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
import respx

from backend.connectors.auth.sentry import SentryProvider

_AUTHZ_URL = "https://sentry.io/api/0/sentry-app-installations/inst-123/authorizations/"


def _provider() -> SentryProvider:
    return SentryProvider(client_id="cid", client_secret="sec")  # noqa: S106


def test_knobs() -> None:
    p = _provider()
    assert p.name == "sentry"
    assert p.refreshable is True
    assert p.supports_service_token is True


@respx.mock
async def test_exchange_installation_mints_token() -> None:
    route = respx.post(_AUTHZ_URL).mock(
        return_value=httpx.Response(
            201,
            json={
                "token": "sntrys_tok",
                "refreshToken": "sntrys_ref",
                "expiresAt": "2099-01-01T00:00:00Z",
                "scopes": ["org:read", "event:read"],
            },
        )
    )
    tok = await _provider().exchange_installation(installation_id="inst-123", code="grant-code")
    assert tok.access_token == "sntrys_tok"
    assert tok.refresh_token == "sntrys_ref"
    assert tok.scopes == ("org:read", "event:read")
    assert tok.expires_at is not None
    assert tok.expires_at > datetime.now(tz=UTC)

    body = route.calls.last.request
    assert body.headers["content-type"].startswith("application/json")
    import json  # noqa: PLC0415

    sent = json.loads(body.content)
    assert sent["grant_type"] == "authorization_code"
    assert sent["code"] == "grant-code"
    assert sent["client_id"] == "cid"
    assert sent["client_secret"] == "sec"


@respx.mock
async def test_refresh_installation_rotates() -> None:
    route = respx.post(_AUTHZ_URL).mock(
        return_value=httpx.Response(201, json={"token": "new", "refreshToken": "newref"})
    )
    tok = await _provider().refresh_installation(installation_id="inst-123", refresh_token="old")
    assert tok.access_token == "new"
    import json  # noqa: PLC0415

    sent = json.loads(route.calls.last.request.content)
    assert sent["grant_type"] == "refresh_token"
    assert sent["refresh_token"] == "old"


async def test_generic_flow_methods_raise() -> None:
    p = _provider()
    with pytest.raises(NotImplementedError):
        p.authorize_url(state="s", code_challenge="c", redirect_uri="r")
    with pytest.raises(NotImplementedError):
        await p.exchange_code(code="c", code_verifier="v", redirect_uri="r")
    with pytest.raises(NotImplementedError):
        await p.refresh(refresh_token="r")
