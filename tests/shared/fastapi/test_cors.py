"""Tests for the CORS middleware helper.

``add_cors_middleware`` is the single point all four products migrate to
during Phase A — it wraps ``starlette.middleware.cors.CORSMiddleware``
with the BSVibe defaults (credentials on, JSON+auth headers, common
methods) and consumes ``cors_allowed_origins`` from
:class:`bsvibe_fastapi.settings.FastApiSettings`.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.cors import CORSMiddleware

from backend.shared.fastapi.cors import add_cors_middleware
from backend.shared.fastapi.settings import FastApiSettings


def _build_settings(origins: list[str]) -> FastApiSettings:
    s = FastApiSettings()
    # Force the field to a known value irrespective of the environment.
    s.cors_allowed_origins = list(origins)
    return s


def test_add_cors_middleware_registers_corsmiddleware() -> None:
    app = FastAPI()
    add_cors_middleware(app, _build_settings(["http://a.test"]))
    classes = [m.cls for m in app.user_middleware]
    assert CORSMiddleware in classes


def test_add_cors_middleware_uses_settings_origins() -> None:
    app = FastAPI()
    add_cors_middleware(app, _build_settings(["http://a.test", "http://b.test"]))
    cors_mw = next(m for m in app.user_middleware if m.cls is CORSMiddleware)
    assert cors_mw.kwargs["allow_origins"] == ["http://a.test", "http://b.test"]


def test_add_cors_middleware_sane_defaults() -> None:
    app = FastAPI()
    add_cors_middleware(app, _build_settings(["http://a.test"]))
    cors_mw = next(m for m in app.user_middleware if m.cls is CORSMiddleware)
    assert cors_mw.kwargs["allow_credentials"] is True
    assert "GET" in cors_mw.kwargs["allow_methods"]
    assert "POST" in cors_mw.kwargs["allow_methods"]
    assert "Authorization" in cors_mw.kwargs["allow_headers"]


def test_preflight_request_returns_cors_headers() -> None:
    app = FastAPI()
    add_cors_middleware(app, _build_settings(["http://a.test"]))

    @app.get("/ping")
    async def ping() -> dict[str, str]:
        return {"ok": "yes"}

    client = TestClient(app)
    resp = client.options(
        "/ping",
        headers={
            "Origin": "http://a.test",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "Authorization",
        },
    )
    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == "http://a.test"
    assert resp.headers["access-control-allow-credentials"] == "true"


def test_preflight_disallowed_origin_omits_allow_origin_header() -> None:
    app = FastAPI()
    add_cors_middleware(app, _build_settings(["http://a.test"]))

    @app.get("/ping")
    async def ping() -> dict[str, str]:
        return {"ok": "yes"}

    client = TestClient(app)
    resp = client.options(
        "/ping",
        headers={
            "Origin": "http://EVIL.test",
            "Access-Control-Request-Method": "GET",
        },
    )
    # Starlette CORSMiddleware drops the allow-origin header when the
    # origin does not match — products rely on this behaviour.
    assert "access-control-allow-origin" not in resp.headers


def test_explicit_origins_argument_overrides_settings() -> None:
    """``add_cors_middleware`` accepts an explicit override for tests.

    Useful for products that compute origins dynamically (e.g. BSGateway
    falling back to ``http://localhost:{api_port}``) without rebuilding
    the whole settings object.
    """

    app = FastAPI()
    add_cors_middleware(
        app,
        _build_settings(["http://a.test"]),
        allow_origins=["http://override.test"],
    )
    cors_mw = next(m for m in app.user_middleware if m.cls is CORSMiddleware)
    assert cors_mw.kwargs["allow_origins"] == ["http://override.test"]
