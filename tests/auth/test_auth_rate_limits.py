"""가입·로그인·재설정의 백엔드 자체 요청 제한.

GoTrue 가 보는 IP 는 백엔드 하나라서, 제한이 없으면 한 사람이 모든 사용자의 GoTrue 한도를 쓴다.
여기서는 GoTrue 호출 지점(`FakeSupabaseClient`)만 가짜로 두고 HTTP 라우트는 실제로 태운다.
"""

from __future__ import annotations

import httpx
import pytest
import pytest_asyncio

from backend.api.deps import get_db_session
from backend.api.main import create_app
from backend.auth.client import SupabaseAuthClient, get_supabase_client
from backend.config import get_settings

CF = "CF-Connecting-IP"


class Gotrue:
    """The HTTP layer only: counts what actually reaches GoTrue."""

    def __init__(self) -> None:
        self.requests: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request.url.path)
        if request.url.path.endswith("/recover"):
            return httpx.Response(200, json={})
        if b"wrong" in request.content:
            return httpx.Response(
                400, json={"error_code": "invalid_credentials", "msg": "Invalid login credentials"}
            )
        return httpx.Response(
            200,
            json={
                "access_token": "a",
                "refresh_token": "r",
                "expires_in": 3600,
                "user": {"id": "sb-1", "email": "u@example.com"},
            },
        )


@pytest.fixture
def gotrue() -> Gotrue:
    return Gotrue()


@pytest_asyncio.fixture
async def client(session_factory, gotrue: Gotrue):  # type: ignore[no-untyped-def]
    app = create_app()

    async def _session():  # type: ignore[no-untyped-def]
        async with session_factory() as s:
            yield s

    real = SupabaseAuthClient(
        base_url="https://fake-supabase",
        publishable_key="pk-test",
        http=httpx.AsyncClient(transport=httpx.MockTransport(gotrue.handler)),
    )
    app.dependency_overrides[get_db_session] = _session
    app.dependency_overrides[get_supabase_client] = lambda: real
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


@pytest.fixture(autouse=True)
def _small_budgets(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_rate_limit_ip_per_minute", 3)
    monkeypatch.setattr(settings, "auth_rate_limit_email_per_hour", 2)
    monkeypatch.setattr(settings, "auth_login_failures_per_email_per_hour", 3)


def _login(
    client: httpx.AsyncClient,
    *,
    ip: str,
    email: str,
    extra: dict | None = None,
    password: str = "pw",
):
    return client.post(
        "/api/auth/login",
        json={"email": email, "password": password},
        headers={CF: ip, **(extra or {})},
    )


async def test_the_same_ip_is_cut_off_and_gotrue_is_not_called(
    client: httpx.AsyncClient, gotrue: Gotrue
) -> None:
    for i in range(3):
        assert (
            await _login(client, ip="203.0.113.1", email=f"u{i}@example.com")
        ).status_code == 200
    resp = await _login(client, ip="203.0.113.1", email="u9@example.com")
    assert resp.status_code == 429
    assert int(resp.headers["Retry-After"]) > 0
    assert len(gotrue.requests) == 3


async def test_a_different_ip_is_unaffected(client: httpx.AsyncClient) -> None:
    for i in range(3):
        await _login(client, ip="203.0.113.1", email=f"u{i}@example.com")
    assert (await _login(client, ip="203.0.113.1", email="x@example.com")).status_code == 429
    assert (await _login(client, ip="203.0.113.2", email="x@example.com")).status_code == 200


async def test_a_forged_x_forwarded_for_does_not_buy_a_new_bucket(
    client: httpx.AsyncClient, gotrue: Gotrue
) -> None:
    for i in range(3):
        resp = await _login(
            client,
            ip="203.0.113.1",
            email=f"u{i}@example.com",
            extra={"X-Forwarded-For": f"9.9.9.{i}, 203.0.113.1"},
        )
        assert resp.status_code == 200
    resp = await _login(
        client,
        ip="203.0.113.1",
        email="late@example.com",
        extra={"X-Forwarded-For": "8.8.8.8"},
    )
    assert resp.status_code == 429
    assert len(gotrue.requests) == 3


async def test_the_signup_email_budget_is_shared_across_case_and_ips(
    client: httpx.AsyncClient, gotrue: Gotrue
) -> None:
    for ip, email in (("198.51.100.1", "Bob@Example.com"), ("198.51.100.2", " bob@example.com ")):
        resp = await client.post(
            "/api/auth/signup", json={"email": email, "password": "pw"}, headers={CF: ip}
        )
        assert resp.status_code == 200
    resp = await client.post(
        "/api/auth/signup",
        json={"email": "BOB@EXAMPLE.COM", "password": "pw"},
        headers={CF: "198.51.100.3"},
    )
    assert resp.status_code == 429
    assert "Retry-After" in resp.headers
    assert len(gotrue.requests) == 2


async def test_successful_logins_beyond_the_failure_budget_all_return_200(
    client: httpx.AsyncClient, gotrue: Gotrue, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "auth_rate_limit_ip_per_minute", 1000)
    for i in range(6):  # budget is 3 failures; 6 successes from several "devices"
        resp = await _login(client, ip=f"198.51.100.{i}", email="multi@example.com")
        assert resp.status_code == 200
    assert len(gotrue.requests) == 6


async def test_failed_logins_reaching_the_budget_return_429_without_gotrue(
    client: httpx.AsyncClient, gotrue: Gotrue, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "auth_rate_limit_ip_per_minute", 1000)
    for i in range(3):
        resp = await _login(client, ip=f"198.51.100.{i}", email="v@example.com", password="wrong")
        assert resp.status_code == 401
    resp = await _login(client, ip="198.51.100.9", email="V@Example.com", password="wrong")
    assert resp.status_code == 429
    assert int(resp.headers["Retry-After"]) > 0
    assert len(gotrue.requests) == 3


async def test_a_successful_login_resets_the_failure_count(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "auth_rate_limit_ip_per_minute", 1000)
    for _ in range(2):
        await _login(client, ip="198.51.100.1", email="r@example.com", password="wrong")
    assert (await _login(client, ip="198.51.100.1", email="r@example.com")).status_code == 200
    for _ in range(2):  # would be the 3rd/4th failure had the success not cleared the record
        resp = await _login(client, ip="198.51.100.1", email="r@example.com", password="wrong")
        assert resp.status_code == 401


async def test_signup_is_limited_before_gotrue(client: httpx.AsyncClient, gotrue: Gotrue) -> None:
    body = {"email": "new@example.com", "password": "pw"}
    for _ in range(2):
        resp = await client.post("/api/auth/signup", json=body, headers={CF: "203.0.113.5"})
        assert resp.status_code == 200
    resp = await client.post("/api/auth/signup", json=body, headers={CF: "203.0.113.6"})
    assert resp.status_code == 429
    assert len(gotrue.requests) == 2


async def test_reset_stays_204_but_is_not_sent_once_limited(
    client: httpx.AsyncClient, gotrue: Gotrue
) -> None:
    body = {"email": "victim@example.com"}
    for i in range(4):
        resp = await client.post(
            "/api/auth/password/reset", json=body, headers={CF: f"203.0.113.{20 + i}"}
        )
        assert resp.status_code == 204
    assert len(gotrue.requests) == 2


async def test_reset_ip_limit_stays_204_and_is_not_sent(
    client: httpx.AsyncClient, gotrue: Gotrue
) -> None:
    for i in range(4):
        resp = await client.post(
            "/api/auth/password/reset",
            json={"email": f"user{i}@example.com"},
            headers={CF: "203.0.113.9"},
        )
        assert resp.status_code == 204
    assert len(gotrue.requests) == 3
