"""Tests for OAuth2 client_credentials ServiceTokenMinter."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx
import pytest

from backend.shared.authz.service_token_minter import (
    ServiceTokenMinter,
    ServiceTokenMinterError,
)


def make_handler(
    response: dict[str, Any] | None = None,
    *,
    status: int = 200,
    capture: list[dict[str, Any]] | None = None,
) -> httpx.MockTransport:
    response = response or {"access_token": "tok-1", "expires_in": 3600}

    def handler(request: httpx.Request) -> httpx.Response:
        body = {}
        if request.headers.get("content-type", "").startswith("application/x-www-form-urlencoded"):
            body = dict(httpx.QueryParams(request.content.decode()))
        if capture is not None:
            capture.append(
                {
                    "url": str(request.url),
                    "method": request.method,
                    "authorization": request.headers.get("authorization"),
                    "body": body,
                }
            )
        return httpx.Response(status, json=response)

    return httpx.MockTransport(handler)


@pytest.fixture
def base_kwargs():
    return {
        "auth_url": "https://auth.bsvibe.dev",
        "client_id": "bsgateway-prod",
        "client_secret": "the-long-random-secret",
        "audience": "bsupervisor",
        "scope": ["bsupervisor:write"],
    }


@pytest.mark.asyncio
async def test_get_token_mints_via_oauth_token_endpoint(base_kwargs):
    capture: list[dict[str, Any]] = []
    transport = make_handler(capture=capture)
    minter = ServiceTokenMinter(**base_kwargs, transport=transport)

    tok = await minter.get_token()

    assert tok == "tok-1"
    assert len(capture) == 1
    call = capture[0]
    assert call["url"].endswith("/api/oauth/token")
    assert call["method"] == "POST"
    assert call["authorization"].startswith("Basic ")
    assert call["body"] == {
        "grant_type": "client_credentials",
        "audience": "bsupervisor",
        "scope": "bsupervisor:write",
    }


@pytest.mark.asyncio
async def test_get_token_caches_until_expiry(base_kwargs):
    capture: list[dict[str, Any]] = []
    transport = make_handler(capture=capture)
    minter = ServiceTokenMinter(**base_kwargs, transport=transport)

    a = await minter.get_token()
    b = await minter.get_token()

    assert a == b == "tok-1"
    assert len(capture) == 1, "second call must hit the cache"


@pytest.mark.asyncio
async def test_invalidate_forces_remint(base_kwargs):
    capture: list[dict[str, Any]] = []
    transport = make_handler(capture=capture)
    minter = ServiceTokenMinter(**base_kwargs, transport=transport)

    await minter.get_token()
    minter.invalidate()
    await minter.get_token()

    assert len(capture) == 2


@pytest.mark.asyncio
async def test_get_token_remints_after_safety_margin(monkeypatch, base_kwargs):
    capture: list[dict[str, Any]] = []
    transport = make_handler(
        response={"access_token": "tok-fresh", "expires_in": 70},
        capture=capture,
    )
    minter = ServiceTokenMinter(
        **base_kwargs,
        transport=transport,
        safety_margin_s=60,
    )

    fixed = [time.time()]
    monkeypatch.setattr("backend.shared.authz.service_token_minter.time.time", lambda: fixed[0])

    await minter.get_token()
    fixed[0] += 15  # well within (expires_in - safety_margin) = 10
    assert len(capture) == 1
    fixed[0] += 60  # now > expiry - safety_margin
    await minter.get_token()
    assert len(capture) == 2


@pytest.mark.asyncio
async def test_concurrent_callers_only_mint_once(base_kwargs):
    capture: list[dict[str, Any]] = []

    async def slow_handler(request: httpx.Request) -> httpx.Response:
        capture.append({"url": str(request.url)})
        await asyncio.sleep(0.05)
        return httpx.Response(200, json={"access_token": "tok-1", "expires_in": 3600})

    transport = httpx.MockTransport(slow_handler)
    minter = ServiceTokenMinter(**base_kwargs, transport=transport)

    results = await asyncio.gather(*(minter.get_token() for _ in range(5)))
    assert all(r == "tok-1" for r in results)
    assert len(capture) == 1


@pytest.mark.asyncio
async def test_http_4xx_raises_minter_error(base_kwargs):
    transport = make_handler(
        response={"error": "invalid_client"},
        status=401,
    )
    minter = ServiceTokenMinter(**base_kwargs, transport=transport)
    with pytest.raises(ServiceTokenMinterError):
        await minter.get_token()


@pytest.mark.asyncio
async def test_malformed_response_raises_minter_error(base_kwargs):
    transport = make_handler(response={"access_token": "tok-1"})  # no expires_in
    minter = ServiceTokenMinter(**base_kwargs, transport=transport)
    with pytest.raises(ServiceTokenMinterError):
        await minter.get_token()


@pytest.mark.asyncio
async def test_request_error_raises_minter_error(base_kwargs):
    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("dns blew up")

    transport = httpx.MockTransport(handler)
    minter = ServiceTokenMinter(**base_kwargs, transport=transport)
    with pytest.raises(ServiceTokenMinterError):
        await minter.get_token()


def test_invalid_audience_rejected_at_construction():
    with pytest.raises(ValueError, match="audience"):
        ServiceTokenMinter(
            auth_url="https://x",
            client_id="c",
            client_secret="s",
            audience="not-a-real-aud",
            scope=["whatever.read"],
        )


def test_invalid_scope_rejected_at_construction():
    with pytest.raises(ValueError, match="scope"):
        ServiceTokenMinter(
            auth_url="https://x",
            client_id="c",
            client_secret="s",
            audience="bsupervisor",
            scope=["bsage:read"],  # wrong audience prefix
        )
    with pytest.raises(ValueError, match="scope"):
        ServiceTokenMinter(
            auth_url="https://x",
            client_id="c",
            client_secret="s",
            audience="bsupervisor",
            scope=[],
        )
    with pytest.raises(ValueError, match="scope"):
        ServiceTokenMinter(
            auth_url="https://x",
            client_id="c",
            client_secret="s",
            audience="bsupervisor",
            scope=["bsupervisor:"],  # malformed
        )


@pytest.mark.asyncio
async def test_basic_auth_header_uses_client_credentials(base_kwargs):
    """Authorization header must encode client_id:client_secret in base64."""
    import base64

    capture: list[dict[str, Any]] = []
    transport = make_handler(capture=capture)
    minter = ServiceTokenMinter(**base_kwargs, transport=transport)
    await minter.get_token()

    auth = capture[0]["authorization"]
    assert auth.startswith("Basic ")
    decoded = base64.b64decode(auth[len("Basic ") :]).decode()
    assert decoded == "bsgateway-prod:the-long-random-secret"
