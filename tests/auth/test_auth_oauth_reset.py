"""/api/auth/oauth/{provider}/authorize + /api/auth/password/reset.

Social sign-in start (assemble the Supabase GoTrue authorize URL with the
caller's PKCE ``code_challenge``) and the password-recover request. Supabase is
mocked via ``FakeSupabaseClient``; the real URL-assembly + recover HTTP are
unit-tested separately against ``SupabaseAuthClient`` with an httpx transport.
"""

from __future__ import annotations

import json as jsonlib
from urllib.parse import parse_qs, urlparse

import httpx

from backend.auth.client import SupabaseAuthClient, SupabaseAuthError

# asyncio_mode = "auto" (pyproject) collects async tests automatically; the one
# sync unit test below (URL assembly) must NOT carry an asyncio mark.

# The test settings default ``cors_allowed_origins`` to ["http://localhost:3700"],
# which is the origin allow-list reused for ``redirect_to`` validation.
ALLOWED_REDIRECT = "http://localhost:3700/auth/callback"
DISALLOWED_REDIRECT = "https://evil.example.com/auth/callback"


# ── /api/auth/oauth/{provider}/authorize ─────────────────────────────────────


async def test_oauth_authorize_returns_supabase_url(client, fake_supabase) -> None:
    r = await client.post(
        "/api/auth/oauth/google/authorize",
        json={"code_challenge": "chal-123", "redirect_to": ALLOWED_REDIRECT},
    )
    assert r.status_code == 200, r.text
    assert r.json()["authorize_url"] == ("https://fake-supabase/auth/v1/authorize?provider=google")
    # The route delegates URL assembly to the Supabase client with the exact args.
    assert fake_supabase.authorize_calls == [("google", ALLOWED_REDIRECT, "chal-123")]


async def test_oauth_authorize_rejects_unknown_provider(client, fake_supabase) -> None:
    r = await client.post(
        "/api/auth/oauth/facebook/authorize",
        json={"code_challenge": "chal-123", "redirect_to": ALLOWED_REDIRECT},
    )
    assert r.status_code == 400, r.text
    assert fake_supabase.authorize_calls == []


async def test_oauth_authorize_rejects_disallowed_redirect(client, fake_supabase) -> None:
    r = await client.post(
        "/api/auth/oauth/google/authorize",
        json={"code_challenge": "chal-123", "redirect_to": DISALLOWED_REDIRECT},
    )
    assert r.status_code == 400, r.text
    assert fake_supabase.authorize_calls == []


async def test_oauth_authorize_validates_payload(client) -> None:
    r = await client.post(
        "/api/auth/oauth/google/authorize", json={"redirect_to": ALLOWED_REDIRECT}
    )
    assert r.status_code == 422


# ── /api/auth/password/reset ─────────────────────────────────────────────────


async def test_password_reset_calls_supabase_and_returns_204(client, fake_supabase) -> None:
    r = await client.post(
        "/api/auth/password/reset",
        json={"email": "founder@example.com", "redirect_to": ALLOWED_REDIRECT},
    )
    assert r.status_code == 204, r.text
    assert fake_supabase.reset_calls == [("founder@example.com", ALLOWED_REDIRECT)]


async def test_password_reset_unknown_email_still_204(client, fake_supabase) -> None:
    """Leak-safe: a GoTrue rejection must not reveal whether the email exists."""
    fake_supabase.reset_error = SupabaseAuthError("boom")
    r = await client.post("/api/auth/password/reset", json={"email": "ghost@example.com"})
    assert r.status_code == 204, r.text
    assert fake_supabase.reset_calls == [("ghost@example.com", None)]


async def test_password_reset_validates_payload(client) -> None:
    r = await client.post("/api/auth/password/reset", json={})
    assert r.status_code == 422


async def test_password_reset_rejects_disallowed_redirect(client, fake_supabase) -> None:
    r = await client.post(
        "/api/auth/password/reset",
        json={"email": "founder@example.com", "redirect_to": DISALLOWED_REDIRECT},
    )
    assert r.status_code == 400, r.text
    assert fake_supabase.reset_calls == []


# ── SupabaseAuthClient unit tests (real assembly / HTTP) ─────────────────────


def test_client_build_authorize_url_assembles_pkce_query() -> None:
    c = SupabaseAuthClient(base_url="https://proj.supabase.co/", publishable_key="sb_pub")
    url = c.build_authorize_url("github", "http://localhost:3700/auth/callback", "the-challenge")
    parsed = urlparse(url)
    assert parsed.scheme == "https"
    assert parsed.netloc == "proj.supabase.co"
    assert parsed.path == "/auth/v1/authorize"
    q = parse_qs(parsed.query)
    assert q["provider"] == ["github"]
    assert q["redirect_to"] == ["http://localhost:3700/auth/callback"]
    assert q["code_challenge"] == ["the-challenge"]
    assert q["code_challenge_method"] == ["s256"]


async def test_client_send_password_reset_posts_to_recover() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = jsonlib.loads(request.content)
        seen["apikey"] = request.headers.get("apikey")
        return httpx.Response(200, json={})

    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport)
    c = SupabaseAuthClient(base_url="https://proj.supabase.co", publishable_key="sb_pub", http=http)

    await c.send_password_reset("founder@example.com", "http://localhost:3700/reset")

    assert seen["url"] == "https://proj.supabase.co/auth/v1/recover"
    assert seen["body"] == {
        "email": "founder@example.com",
        "redirect_to": "http://localhost:3700/reset",
    }
    assert seen["apikey"] == "sb_pub"
