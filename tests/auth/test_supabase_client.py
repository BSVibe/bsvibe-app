"""Unit tests for the Supabase GoTrue client (HTTP mocked with respx)."""

from __future__ import annotations

import httpx
import pytest
import respx

from backend.auth.client import (
    SupabaseAuthClient,
    SupabaseAuthError,
    get_supabase_client,
)
from backend.config import get_settings

pytestmark = pytest.mark.asyncio

BASE = "https://supabase.test"
TOKEN_URL = f"{BASE}/auth/v1/token"
LOGOUT_URL = f"{BASE}/auth/v1/logout"

_GOTRUE_OK = {
    "access_token": "at",
    "refresh_token": "rt",
    "expires_in": 3600,
    "token_type": "bearer",
    "user": {"id": "sb-1", "email": "a@x.io"},
}


def _client() -> SupabaseAuthClient:
    return SupabaseAuthClient(base_url=BASE + "/", anon_key="anon-key")


@respx.mock
async def test_password_login_parses_session() -> None:
    route = respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=_GOTRUE_OK))
    session = await _client().password_login("a@x.io", "pw")
    assert session.access_token == "at"
    assert session.refresh_token == "rt"
    assert session.supabase_user_id == "sb-1"
    assert session.email == "a@x.io"
    assert route.calls.last.request.url.params["grant_type"] == "password"


@respx.mock
async def test_exchange_code_sends_pkce_params() -> None:
    route = respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=_GOTRUE_OK))
    await _client().exchange_code_for_session("the-code", code_verifier="verifier")
    body = route.calls.last.request.content.decode()
    assert "the-code" in body
    assert "verifier" in body
    assert route.calls.last.request.url.params["grant_type"] == "pkce"


@respx.mock
async def test_refresh_uses_refresh_grant() -> None:
    route = respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=_GOTRUE_OK))
    await _client().refresh("old-rt")
    assert route.calls.last.request.url.params["grant_type"] == "refresh_token"


@respx.mock
async def test_token_error_raises() -> None:
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(400, json={"error": "bad"}))
    with pytest.raises(SupabaseAuthError):
        await _client().password_login("a@x.io", "wrong")


@respx.mock
async def test_missing_user_id_raises() -> None:
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "at", "user": {}})
    )
    with pytest.raises(SupabaseAuthError):
        await _client().password_login("a@x.io", "pw")


@respx.mock
@pytest.mark.parametrize("status_code", [200, 204, 401])
async def test_logout_tolerates_terminal_statuses(status_code: int) -> None:
    respx.post(LOGOUT_URL).mock(return_value=httpx.Response(status_code))
    await _client().logout("access")  # no raise


@respx.mock
async def test_logout_raises_on_server_error() -> None:
    respx.post(LOGOUT_URL).mock(return_value=httpx.Response(500))
    with pytest.raises(SupabaseAuthError):
        await _client().logout("access")


async def test_get_supabase_client_is_singleton(monkeypatch) -> None:
    import backend.auth.client as client_mod

    monkeypatch.setattr(client_mod, "_client_singleton", None)
    monkeypatch.setenv("BSVIBE_SUPABASE_URL", BASE)
    monkeypatch.setenv("BSVIBE_SUPABASE_ANON_KEY", "anon")
    get_settings.cache_clear()
    try:
        first = get_supabase_client()
        second = get_supabase_client()
        assert first is second
    finally:
        monkeypatch.setattr(client_mod, "_client_singleton", None)
        get_settings.cache_clear()
