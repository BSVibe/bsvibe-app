"""FastAPI dependency tests — authentication only (get_current_user)."""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from backend.shared.authz.deps import CurrentUser, get_current_user, get_settings_dep
from backend.shared.authz.settings import Settings


def _app(settings: Settings) -> FastAPI:
    app = FastAPI()
    app.dependency_overrides[get_settings_dep] = lambda: settings

    @app.get("/me")
    async def me(user: CurrentUser) -> dict:
        return {"id": user.id, "email": user.email, "is_service": user.is_service}

    return app


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.fixture
def settings(user_jwt_secret: str, issuer: str) -> Settings:
    return Settings(  # type: ignore[call-arg]
        user_jwt_secret=user_jwt_secret,
        user_jwt_algorithm="HS256",
        user_jwt_audience="bsvibe",
        user_jwt_issuer=issuer,
    )


async def test_valid_token_resolves_user(settings, make_user_jwt) -> None:
    app = _app(settings)
    token = make_user_jwt(sub="u-42", email="bob@bsvibe.dev")
    async with _client(app) as c:
        r = await c.get("/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    assert r.json() == {"id": "u-42", "email": "bob@bsvibe.dev", "is_service": False}


async def test_missing_authorization_header_is_401(settings) -> None:
    app = _app(settings)
    async with _client(app) as c:
        r = await c.get("/me")
    assert r.status_code == 401
    assert "missing Authorization" in r.json()["detail"]


async def test_non_bearer_scheme_is_401(settings) -> None:
    app = _app(settings)
    async with _client(app) as c:
        r = await c.get("/me", headers={"Authorization": "Basic abc"})
    assert r.status_code == 401
    assert "scheme" in r.json()["detail"]


async def test_invalid_token_is_401(settings) -> None:
    app = _app(settings)
    async with _client(app) as c:
        r = await c.get("/me", headers={"Authorization": "Bearer not.a.jwt"})
    assert r.status_code == 401


async def test_expired_token_is_401(settings, make_user_jwt) -> None:
    app = _app(settings)
    token = make_user_jwt(exp_offset=-5)
    async with _client(app) as c:
        r = await c.get("/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401


async def test_get_current_user_direct_call_is_authn(settings, make_user_jwt) -> None:
    # Direct (non-FastAPI) invocation still verifies the token.
    token = make_user_jwt(sub="u-direct")
    user = await get_current_user(authorization=f"Bearer {token}", settings=settings)
    assert user.id == "u-direct"


def test_get_settings_dep_returns_settings() -> None:
    assert isinstance(get_settings_dep(), Settings)
