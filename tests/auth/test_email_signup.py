"""이메일 회원가입 — `SupabaseAuthClient.sign_up` + `POST /api/auth/signup` (#937 게이트2).

로그인·비밀번호 재설정은 이미 있고 **가입만 없었다**. 감사(§게이트2)의 문장 그대로다:
*"이메일 가입 UI/API 없음(로그인만) — 신규 진입은 Google/GitHub OAuth 뿐"*.

이 테스트가 지키는 핵심은 **GoTrue 의 `/signup` 응답이 두 모양**이라는 것이다:

* 자동 확인(Confirm email OFF) → `{access_token, refresh_token, user:{...}}` — 로그인과 같은 모양
* 확인 필요(Confirm email ON) → **user 객체가 최상위**, 토큰 없음

기존 `_session_from_gotrue` 는 `body["user"]` 를 찾으므로 **두 번째 모양에서 터진다**.
그래서 가입은 세션을 그냥 반환할 수 없고 *"세션이냐 확인 대기냐"* 를 구분해 돌려줘야 한다.

⚠️ 그리고 지금 prod 의 Supabase 는 이메일 가입이 **꺼져 있다.** 이 기능은 켜질 때까지
inert 이어야 하고, 그때 나오는 에러가 **500 이 아니라 읽을 수 있는 거절**이어야 한다 —
그 칸이 없으면 "왜 가입이 안 되지"가 서버 버그처럼 보인다.
"""

from __future__ import annotations

import httpx
import pytest

from backend.auth.client import SupabaseAuthClient, SupabaseAuthError

ALLOWED_REDIRECT = "http://localhost:3700/auth/callback"
DISALLOWED_REDIRECT = "https://evil.example.com/auth/callback"


def _client(handler) -> SupabaseAuthClient:
    transport = httpx.MockTransport(handler)
    return SupabaseAuthClient(
        base_url="https://fake-supabase",
        publishable_key="pk-test",
        http=httpx.AsyncClient(transport=transport),
    )


# ── 클라이언트 ────────────────────────────────────────────────────────────────


async def test_sign_up_posts_email_and_password_to_the_signup_endpoint() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = httpx.Response(200, json={}).json() if False else request.read().decode()
        return httpx.Response(
            200,
            json={
                "access_token": "at",
                "refresh_token": "rt",
                "user": {"id": "sb-1", "email": "new@example.com"},
            },
        )

    result = await _client(handler).sign_up("new@example.com", "pw-12345678")

    assert seen["url"] == "https://fake-supabase/auth/v1/signup"
    assert "new@example.com" in str(seen["body"])
    assert result.confirmation_required is False
    assert result.session is not None
    assert result.session.supabase_user_id == "sb-1"


async def test_sign_up_reports_confirmation_required_when_gotrue_returns_no_tokens() -> None:
    """확인 메일 모드의 응답은 **user 가 최상위**이고 토큰이 없다.

    이게 이 기능의 유일하게 까다로운 지점이다. 기존 파서는 `body["user"]` 를
    찾으므로 이 모양에서 *"missing user id"* 로 터진다 — 즉 정상 가입이
    에러로 보고된다.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            json={"id": "sb-2", "email": "pending@example.com", "confirmation_sent_at": "now"},
        )

    result = await _client(handler).sign_up("pending@example.com", "pw-12345678")

    assert result.confirmation_required is True
    assert result.session is None


async def test_sign_up_raises_when_signups_are_disabled() -> None:
    """콘솔에서 이메일 가입이 꺼져 있으면 GoTrue 가 422 로 거절한다."""

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(422, json={"msg": "Signups not allowed for this instance"})

    with pytest.raises(SupabaseAuthError):
        await _client(handler).sign_up("nope@example.com", "pw-12345678")


# ── 라우트 ────────────────────────────────────────────────────────────────────


async def test_signup_route_returns_a_session_and_bootstraps_the_user(
    client, fake_supabase, db_session
) -> None:
    from sqlalchemy import select

    from backend.identity.db import UserRow

    r = await client.post(
        "/api/auth/signup",
        json={"email": "new@example.com", "password": "pw-12345678"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["confirmation_required"] is False
    assert body["session"]["access_token"] == "access-token"

    rows = (await db_session.execute(select(UserRow))).scalars().all()
    assert [row.supabase_user_id for row in rows] == [fake_supabase.user_id], (
        "자동 확인 가입은 로그인과 같으므로 사용자 부트스트랩까지 가야 한다"
    )


async def test_signup_route_does_not_bootstrap_while_confirmation_is_pending(
    client, fake_supabase, db_session
) -> None:
    """확인 대기 상태에서는 **행을 만들면 안 된다**.

    아직 이메일 소유가 증명되지 않았다. 여기서 부트스트랩하면 남의 주소로
    워크스페이스를 선점할 수 있다.
    """
    from sqlalchemy import select

    from backend.identity.db import UserRow

    fake_supabase.signup_confirmation_required = True

    r = await client.post(
        "/api/auth/signup",
        json={"email": "pending@example.com", "password": "pw-12345678"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["confirmation_required"] is True
    assert r.json()["session"] is None

    rows = (await db_session.execute(select(UserRow))).scalars().all()
    assert rows == [], "확인 전에 사용자 행이 생겼다 — 이메일 소유가 증명되기 전이다"


async def test_signup_route_surfaces_a_legible_refusal_when_signups_are_disabled(
    client, fake_supabase
) -> None:
    """가입이 **실제로** 꺼진 경우에만 403 이다.

    ⚠️ 이 테스트는 원래 이유 없는 `SupabaseAuthError` 를 던져 403 을 기대했다 —
    즉 *"뭐든 거절되면 문이 닫힌 것"* 이라는 결함을 그대로 못박고 있었다. 그리고
    그 결함이 2026-09-18 에 나를 속였다(403 을 보고 "가입이 꺼져 있다"고 했는데
    실제로는 레이트리밋이었고 가입은 켜져 있었다). 이유를 명시하도록 바꿨다 —
    이유별 분기는 `test_signup_error_reasons.py` 가 덮는다.
    """
    fake_supabase.signup_error = SupabaseAuthError(
        "signups disabled", status=422, error_code="signup_disabled"
    )

    r = await client.post(
        "/api/auth/signup",
        json={"email": "nope@example.com", "password": "pw-12345678"},
    )
    assert r.status_code == 403, r.text
    assert "signup" in r.json()["detail"].lower()


async def test_signup_route_rejects_an_offsite_redirect(client, fake_supabase) -> None:
    """음성 대조군 — 확인 메일 링크의 redirect_to 는 오픈 리다이렉트 통로다."""
    r = await client.post(
        "/api/auth/signup",
        json={
            "email": "new@example.com",
            "password": "pw-12345678",
            "redirect_to": DISALLOWED_REDIRECT,
        },
    )
    assert r.status_code == 400, r.text
    assert fake_supabase.signup_calls == [], "거절당한 요청이 Supabase 까지 갔다"


async def test_signup_route_passes_an_allowed_redirect_through(client, fake_supabase) -> None:
    """양성 대조군 — 허용 오리진은 그대로 전달돼야 한다.

    이게 없으면 위 테스트는 "redirect_to 를 통째로 무시한다"로도 통과한다.
    """
    r = await client.post(
        "/api/auth/signup",
        json={
            "email": "new@example.com",
            "password": "pw-12345678",
            "redirect_to": ALLOWED_REDIRECT,
        },
    )
    assert r.status_code == 200, r.text
    assert fake_supabase.signup_calls == [("new@example.com", ALLOWED_REDIRECT)]
