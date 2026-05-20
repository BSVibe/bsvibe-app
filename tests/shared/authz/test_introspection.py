"""Tests for RFC 7662 IntrospectionClient."""

from __future__ import annotations

import base64
from typing import Any

import httpx
import pytest

from backend.shared.authz.introspection import IntrospectionClient
from backend.shared.authz.types import IntrospectionResponse


def _make_client(
    handler,
    *,
    url: str = "https://auth.bsvibe.dev/api/oauth/introspect",
    client_id: str = "bsgateway-prod",
    client_secret: str = "the-long-random-secret",
    timeout_s: float = 5.0,
) -> tuple[IntrospectionClient, httpx.AsyncClient]:
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport)
    client = IntrospectionClient(
        introspection_url=url,
        client_id=client_id,
        client_secret=client_secret,
        http=http,
        timeout_s=timeout_s,
    )
    return client, http


@pytest.mark.asyncio
async def test_introspect_active_token_returns_full_payload():
    capture: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = dict(httpx.QueryParams(request.content.decode()))
        capture.append(
            {
                "url": str(request.url),
                "method": request.method,
                "authorization": request.headers.get("authorization"),
                "content_type": request.headers.get("content-type"),
                "body": body,
            }
        )
        return httpx.Response(
            200,
            json={
                "active": True,
                "sub": "user-123",
                "tenant": "t-acme",
                "aud": ["bsgateway"],
                "scope": ["bsgateway:models:read", "bsgateway:models:write"],
                "exp": 1_900_000_000,
                "client_id": "cli-xyz",
                "username": "alice@bsvibe.dev",
            },
        )

    client, http = _make_client(handler)
    try:
        result = await client.introspect("opaque-token-abc")
    finally:
        await http.aclose()

    assert isinstance(result, IntrospectionResponse)
    assert result.active is True
    assert result.sub == "user-123"
    assert result.tenant == "t-acme"
    assert result.aud == ["bsgateway"]
    assert result.scope == ["bsgateway:models:read", "bsgateway:models:write"]
    assert result.exp == 1_900_000_000
    assert result.client_id == "cli-xyz"
    assert result.username == "alice@bsvibe.dev"

    assert len(capture) == 1
    call = capture[0]
    assert call["url"] == "https://auth.bsvibe.dev/api/oauth/introspect"
    assert call["method"] == "POST"
    assert call["content_type"].startswith("application/x-www-form-urlencoded")
    assert call["body"] == {"token": "opaque-token-abc", "token_type_hint": "access_token"}
    auth = call["authorization"]
    assert auth.startswith("Basic ")
    decoded = base64.b64decode(auth[len("Basic ") :]).decode()
    assert decoded == "bsgateway-prod:the-long-random-secret"


@pytest.mark.asyncio
async def test_introspect_inactive_short_form():
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"active": False})

    client, http = _make_client(handler)
    try:
        result = await client.introspect("revoked-token")
    finally:
        await http.aclose()

    assert result.active is False
    assert result.sub is None
    assert result.scope is None


@pytest.mark.asyncio
async def test_introspect_http_500_returns_inactive_fallback():
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "internal"})

    client, http = _make_client(handler)
    try:
        result = await client.introspect("any-token")
    finally:
        await http.aclose()

    assert result.active is False


@pytest.mark.asyncio
async def test_introspect_network_error_returns_inactive_fallback():
    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("dns blew up")

    client, http = _make_client(handler)
    try:
        result = await client.introspect("any-token")
    finally:
        await http.aclose()

    assert result.active is False


@pytest.mark.asyncio
async def test_introspect_does_not_log_token_value(caplog):
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    secret_token = "super-secret-token-do-not-leak"
    client, http = _make_client(handler)
    try:
        with caplog.at_level("DEBUG"):
            await client.introspect(secret_token)
    finally:
        await http.aclose()

    for record in caplog.records:
        assert secret_token not in record.getMessage()


@pytest.mark.asyncio
async def test_introspect_malformed_json_returns_inactive_fallback():
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=b"not json", headers={"content-type": "application/json"}
        )

    client, http = _make_client(handler)
    try:
        result = await client.introspect("any-token")
    finally:
        await http.aclose()

    assert result.active is False


@pytest.mark.asyncio
async def test_introspect_schema_violation_returns_inactive_fallback():
    """JSON missing the required ``active`` field must fall back to active=false."""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"sub": "user-123"})  # no `active`

    client, http = _make_client(handler)
    try:
        result = await client.introspect("any-token")
    finally:
        await http.aclose()

    assert result.active is False


def test_construction_rejects_empty_url():
    with pytest.raises(ValueError, match="introspection_url"):
        IntrospectionClient(
            introspection_url="",
            client_id="c",
            client_secret="s",
        )


def test_construction_rejects_empty_credentials():
    with pytest.raises(ValueError, match="client_id and client_secret"):
        IntrospectionClient(
            introspection_url="https://x/introspect",
            client_id="",
            client_secret="s",
        )
    with pytest.raises(ValueError, match="client_id and client_secret"):
        IntrospectionClient(
            introspection_url="https://x/introspect",
            client_id="c",
            client_secret="",
        )


@pytest.mark.asyncio
async def test_introspect_creates_internal_client_when_http_not_provided(monkeypatch):
    """If no http client is injected, IntrospectionClient must build one per call."""
    captured: list[dict[str, Any]] = []

    real_async_client = httpx.AsyncClient

    def fake_async_client(*args, **kwargs):
        transport = httpx.MockTransport(
            lambda req: httpx.Response(200, json={"active": True, "sub": "u"})
        )
        captured.append({"timeout": kwargs.get("timeout")})
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("backend.shared.authz.introspection.httpx.AsyncClient", fake_async_client)

    client = IntrospectionClient(
        introspection_url="https://x/introspect",
        client_id="c",
        client_secret="s",
        timeout_s=2.5,
    )
    result = await client.introspect("tok")
    assert result.active is True
    assert captured[0]["timeout"] == 2.5
