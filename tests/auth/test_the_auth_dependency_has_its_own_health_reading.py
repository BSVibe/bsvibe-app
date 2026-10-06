"""The liveness watch reads the auth dependency without spending sign-in budget.

Since 2026-09-11 (Supabase paused, login broken, ``/api/health`` green) two
probes read "is auth alive?" by POSTing a bogus password to ``/api/auth/login``
— the launchd heartbeat every 60 s and the off-box workflow every 15 min. That
worked, but every reading was a FAILED SIGN-IN against GoTrue:

* prod logged ``supabase_token_failed`` (warning) once a minute, burying any
  real failed login or brute-force attempt in the noise;
* every user's login is proxied from the backend's one IP, so the probe spent
  the same per-IP sign-in rate-limit budget as the users.

GoTrue has a read-only ``/auth/v1/health``. ``GET /api/health/auth`` relays it:
200 when the dependency answers healthy, 503 otherwise — no token request.
"""

from __future__ import annotations

import httpx
import pytest

from backend.api.main import create_app
from backend.auth.client import SupabaseAuthClient, get_supabase_client

pytestmark = pytest.mark.asyncio


def _client(handler) -> tuple[SupabaseAuthClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(record))
    return (
        SupabaseAuthClient(base_url="https://sb.example/", publishable_key="pk", http=http),
        seen,
    )


async def test_a_healthy_gotrue_reads_healthy_without_a_token_request() -> None:
    client, seen = _client(lambda _r: httpx.Response(200, json={"name": "GoTrue"}))

    assert await client.health() is True
    assert [r.url.path for r in seen] == ["/auth/v1/health"]
    assert seen[0].method == "GET"
    assert seen[0].headers["apikey"] == "pk"


@pytest.mark.parametrize("code", [500, 503, 540])
async def test_an_unhealthy_gotrue_reads_unhealthy(code: int) -> None:
    """540 is what a paused Supabase project answers."""
    client, _seen = _client(lambda _r: httpx.Response(code))

    assert await client.health() is False


async def test_an_unreachable_gotrue_reads_unhealthy() -> None:
    """2026-09-11's other face: a paused project vanished from DNS."""

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("name resolution failed", request=request)

    client, _seen = _client(boom)

    assert await client.health() is False


class _Fake:
    def __init__(self, healthy: bool) -> None:
        self.healthy = healthy

    async def health(self) -> bool:
        return self.healthy


async def _get(healthy: bool) -> httpx.Response:
    app = create_app()
    app.dependency_overrides[get_supabase_client] = lambda: _Fake(healthy)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        return await c.get("/api/health/auth")


async def test_the_route_is_200_when_the_dependency_is_healthy() -> None:
    resp = await _get(True)

    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


async def test_the_route_is_503_when_the_dependency_is_down() -> None:
    """503 — the probes treat anything but 200 as DEPENDENCY down."""
    resp = await _get(False)

    assert resp.status_code == 503
