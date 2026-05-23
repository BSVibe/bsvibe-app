"""CORS support — the browser PWA (app.bsvibe.dev) calls api.bsvibe.dev
cross-origin with a Bearer token (no cookies), so the app must answer the
preflight ``OPTIONS`` and echo ``Access-Control-Allow-Origin`` for allowed
origins only.

These tests drive ``create_app()`` over an in-process httpx ASGI transport.
The allowed origins come from ``Settings.cors_allowed_origins``; the tests
inject a known test origin by overriding the env var and clearing the
``get_settings`` lru_cache so the app factory picks the override up.
"""

from __future__ import annotations

import httpx
import pytest

from backend.api.main import create_app
from backend.config import get_settings

_ALLOWED = "http://localhost:3700"
_DISALLOWED = "http://evil.example"


@pytest.fixture
def cors_app(monkeypatch: pytest.MonkeyPatch) -> object:
    monkeypatch.setenv("BSVIBE_CORS_ALLOWED_ORIGINS", _ALLOWED)
    get_settings.cache_clear()
    app = create_app()
    yield app
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_preflight_options_allowed_origin_returns_acao(cors_app: object) -> None:
    transport = httpx.ASGITransport(app=cors_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.options(
            "/api/v1/workspaces",
            headers={
                "Origin": _ALLOWED,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization,content-type",
            },
        )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == _ALLOWED
    allow_methods = response.headers["access-control-allow-methods"]
    assert "POST" in allow_methods
    assert "OPTIONS" in allow_methods
    allow_headers = response.headers["access-control-allow-headers"].lower()
    assert "authorization" in allow_headers
    assert "content-type" in allow_headers


@pytest.mark.asyncio
async def test_preflight_disallowed_origin_has_no_acao(cors_app: object) -> None:
    transport = httpx.ASGITransport(app=cors_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.options(
            "/api/v1/workspaces",
            headers={
                "Origin": _DISALLOWED,
                "Access-Control-Request-Method": "POST",
            },
        )

    assert "access-control-allow-origin" not in response.headers


@pytest.mark.asyncio
async def test_simple_get_allowed_origin_echoes_acao(cors_app: object) -> None:
    transport = httpx.ASGITransport(app=cors_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/health", headers={"Origin": _ALLOWED})

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == _ALLOWED


@pytest.mark.asyncio
async def test_simple_get_disallowed_origin_has_no_acao(cors_app: object) -> None:
    transport = httpx.ASGITransport(app=cors_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/health", headers={"Origin": _DISALLOWED})

    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers


@pytest.mark.asyncio
async def test_credentials_not_allowed(cors_app: object) -> None:
    """Bearer-header auth, not cookies — the allow-credentials header must
    NOT be emitted (a wildcard-free allowlist with credentials would be a
    different posture than this Direct-call design)."""
    transport = httpx.ASGITransport(app=cors_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.options(
            "/api/v1/workspaces",
            headers={
                "Origin": _ALLOWED,
                "Access-Control-Request-Method": "POST",
            },
        )

    assert "access-control-allow-credentials" not in response.headers
