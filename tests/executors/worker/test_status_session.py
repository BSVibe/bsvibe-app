"""``bsvibe status`` 의 세션 판정 — 시각 비교가 실제로 판정을 낸다는 것을 건다.

실측된 결함(2026-09-02): ``status`` 는 ``expires_at`` 을 출력만 하고 지금 시각과
비교하지 않아, 17시간 전에 죽은 토큰에 "Signed in" + exit 0 을 줬다.

⚠️ 여기 테스트는 **과거/미래 두 자격증명이 서로 다른 결과를 낸다**는 것을 건다.
한쪽만 걸면 "언제나 Signed in" 도, "언제나 EXPIRED" 도 영원히 통과한다.

판정 함수는 순수하다 — ``now`` 를 인자로 받으므로 시계를 가로챌 필요가 없고,
자격증명은 그대로 만들어 넣는다.
"""

from __future__ import annotations

from backend.executors.worker.cli import SessionState, evaluate_session
from backend.executors.worker.credentials import HostCredentials

NOW = 1_788_194_714.0


def _creds(*, expires_at: int | None, refresh_token: str | None = None) -> HostCredentials:
    return HostCredentials(
        access_token="ACC",
        refresh_token=refresh_token,
        expires_at=expires_at,
        issuer="https://api.bsvibe.dev",
    )


def test_past_and_future_expiry_disagree() -> None:
    """같은 시각에 대해 과거/미래가 **다른** 판정과 **다른** exit code 를 낸다."""
    future = evaluate_session(_creds(expires_at=int(NOW) + 3600), now=NOW)
    past = evaluate_session(_creds(expires_at=int(NOW) - 3600), now=NOW)

    assert future.state is SessionState.SIGNED_IN
    assert past.state is not SessionState.SIGNED_IN
    assert future.exit_code != past.exit_code


def test_valid_token_is_signed_in_with_remaining_time() -> None:
    status = evaluate_session(_creds(expires_at=int(NOW) + 5400), now=NOW)
    assert status.state is SessionState.SIGNED_IN
    assert status.exit_code == 0
    body = "\n".join(status.lines)
    assert "Signed in." in body
    assert "1h 30m" in body  # 남은 시간을 사람이 읽을 수 있게


def test_expired_without_refresh_token_demands_relogin() -> None:
    status = evaluate_session(_creds(expires_at=int(NOW) - 62_280), now=NOW)
    assert status.state is SessionState.EXPIRED_REAUTH
    assert status.exit_code == 2
    body = "\n".join(status.lines)
    assert "EXPIRED" in body
    assert "bsvibe login" in body  # 무엇을 하면 되는지가 화면에 있어야 한다
    assert "17h 18m" in body


def test_expired_with_refresh_token_is_its_own_state() -> None:
    status = evaluate_session(_creds(expires_at=int(NOW) - 3600, refresh_token="RT"), now=NOW)
    assert status.state is SessionState.EXPIRED_REFRESHABLE
    assert status.exit_code == 3
    body = "\n".join(status.lines)
    assert "refresh token" in body
    assert "bsvibe login" in body


def test_refresh_token_only_matters_once_expired() -> None:
    """유효한 동안에는 refresh_token 유무가 판정을 바꾸지 않는다."""
    with_rt = evaluate_session(_creds(expires_at=int(NOW) + 3600, refresh_token="RT"), now=NOW)
    without_rt = evaluate_session(_creds(expires_at=int(NOW) + 3600), now=NOW)
    assert with_rt.state is SessionState.SIGNED_IN
    assert without_rt.state is SessionState.SIGNED_IN


def test_unknown_expiry_is_not_treated_as_expired() -> None:
    """``expires_at is None`` 은 **모르는 것**이지 죽은 것이 아니다."""
    status = evaluate_session(_creds(expires_at=None), now=NOW)
    assert status.state is SessionState.SIGNED_IN
    assert status.exit_code == 0
    assert "unknown" in "\n".join(status.lines)


def test_exact_expiry_instant_counts_as_expired() -> None:
    """경계: ``now == expires_at`` 이면 이미 못 쓴다."""
    status = evaluate_session(_creds(expires_at=int(NOW)), now=NOW)
    assert status.state is SessionState.EXPIRED_REAUTH


def test_missing_credentials_keep_exit_one_and_carry_the_reason() -> None:
    status = evaluate_session(None, now=NOW, detail="no credentials at /tmp/x.json")
    assert status.state is SessionState.NOT_SIGNED_IN
    assert status.exit_code == 1
    body = "\n".join(status.lines)
    assert "/tmp/x.json" in body
    assert "bsvibe login" in body


def test_every_state_has_a_distinct_exit_code() -> None:
    """상태와 exit code 는 1:1 — 스크립트가 게이트를 걸 수 있어야 한다."""
    codes = [int(state) for state in SessionState]
    assert sorted(codes) == [0, 1, 2, 3]
