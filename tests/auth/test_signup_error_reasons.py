"""가입 실패의 **이유**를 구분한다 (#937).

PR #1004 가 GoTrue 의 **모든** 거절을 하나로 뭉갰다:

    except SupabaseAuthError:
        raise HTTPException(403, "signup is not available for this instance")

그래서 레이트리밋도, 잘못된 이메일도, 약한 비밀번호도 전부 *"가입이 안 열렸습니다"*
가 됐다. **고칠 수 있는 문제를 고칠 수 없는 문제처럼 보여준 것**이다.

🚨 그리고 이 거짓말에 **내가 직접 속았다.** 2026-09-18 prod 프로브가 403 을 받고
*"이메일 가입이 꺼져 있다"* 로 결론 냈는데, GoTrue 에 직접 물어보니
`429 over_email_send_rate_limit` 이었다 — **확인 메일을 보내려다** 걸린 것이고,
즉 **가입은 처음부터 켜져 있었다.** 형님이 *"이미 enabled 되어 있어"* 라고
정정해 주고 나서야 잡혔다.

⇒ 상태 코드는 이유 코드가 아니다. GoTrue 는 `error_code` 를 주는데 우리가 버렸다.
"""

from __future__ import annotations

import httpx
import pytest

from backend.auth.client import SupabaseAuthClient, SupabaseAuthError


def _client(handler) -> SupabaseAuthClient:
    return SupabaseAuthClient(
        base_url="https://fake-supabase",
        publishable_key="pk-test",
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


def _gotrue(status: int, error_code: str, msg: str = "nope"):
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(status, json={"code": status, "error_code": error_code, "msg": msg})

    return handler


# ── 클라이언트: 이유를 실어 보낸다 ────────────────────────────────────────────


async def test_the_error_carries_gotrue_reason_and_status() -> None:
    """`error_code` 와 status 가 예외에 남아야 라우트가 가를 수 있다."""
    with pytest.raises(SupabaseAuthError) as caught:
        await _client(_gotrue(429, "over_email_send_rate_limit")).sign_up("a@b.com", "pw-12345678")

    assert caught.value.status == 429
    assert caught.value.error_code == "over_email_send_rate_limit"


async def test_a_missing_error_code_does_not_crash() -> None:
    """GoTrue 가 형태를 바꿔도(또는 HTML 을 줘도) 죽지 않아야 한다."""

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(500, text="<html>gateway</html>")

    with pytest.raises(SupabaseAuthError) as caught:
        await _client(handler).sign_up("a@b.com", "pw-12345678")

    assert caught.value.status == 500
    assert caught.value.error_code is None


# ── 라우트: 이유마다 다른 답 ──────────────────────────────────────────────────


async def test_signups_disabled_is_403(client, fake_supabase) -> None:
    fake_supabase.signup_error = SupabaseAuthError(
        "disabled", status=422, error_code="signup_disabled"
    )
    r = await client.post("/api/auth/signup", json={"email": "a@b.com", "password": "pw-12345678"})
    assert r.status_code == 403, r.text


async def test_rate_limit_is_429_not_a_closed_door(client, fake_supabase) -> None:
    """오늘 나를 속인 바로 그 경우다. 403 이면 '안 열렸다'로 읽힌다."""
    fake_supabase.signup_error = SupabaseAuthError(
        "rate", status=429, error_code="over_email_send_rate_limit"
    )
    r = await client.post("/api/auth/signup", json={"email": "a@b.com", "password": "pw-12345678"})
    assert r.status_code == 429, r.text
    assert "403" not in r.text


async def test_a_bad_input_is_400_so_the_user_can_fix_it(client, fake_supabase) -> None:
    fake_supabase.signup_error = SupabaseAuthError(
        "bad email", status=400, error_code="validation_failed"
    )
    r = await client.post("/api/auth/signup", json={"email": "a@b.com", "password": "pw-12345678"})
    assert r.status_code == 400, r.text


async def test_an_unknown_reason_is_not_reported_as_a_closed_door(client, fake_supabase) -> None:
    """모르는 이유를 403 으로 뭉개면 #1004 의 결함이 그대로 돌아온다.

    ⚠️ 이게 **음성 대조군**이다 — 위 세 테스트는 '아는 코드 세 개'만 덮는다.
    기본값이 403 이면 그 셋만 고쳐도 나머지 전부가 여전히 거짓말한다.
    """
    fake_supabase.signup_error = SupabaseAuthError(
        "who knows", status=500, error_code="something_new"
    )
    r = await client.post("/api/auth/signup", json={"email": "a@b.com", "password": "pw-12345678"})
    assert r.status_code != 403, f"모르는 이유가 '가입 비활성'으로 보고됐다: {r.text}"
