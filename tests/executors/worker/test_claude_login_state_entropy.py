"""The authorize `state` must carry the entropy the endpoint requires (#965).

MEASURED end-to-end on 2026-09-17, three URLs opened in a logged-in browser,
identical in all eight parameters except `state`:

    real CLI  (state 43 chars)  -> authorize page OK
    ours      (state 22 chars)  -> "Invalid request format"
    ours      (state 43 chars)  -> authorize page OK

So the endpoint rejects the short one. Ours came from
``secrets.token_urlsafe(16)`` — 22 chars — while the real CLI sends 43, which is
``token_urlsafe(32)``. That is the whole defect: it never reached the token step,
and the failure named the *format*, not the field.

The length is the pin here, not the exact generator: 43 characters is what a
32-byte urlsafe token is, and it is what was measured to pass.
"""

from __future__ import annotations

from typing import Any

#: base64url of 32 random bytes — what `claude setup-token` was measured to send.
REAL_CLI_STATE_LEN = 43


def _fake_exchanger(rec: dict[str, Any]):
    def _exchange(*, code: str, code_verifier: str, redirect_uri: str, state: str) -> dict:
        rec.update(code=code, code_verifier=code_verifier, redirect_uri=redirect_uri, state=state)
        return {"access_token": "a", "refresh_token": "r", "expires_in": 3600}

    return _exchange


def test_the_default_state_is_long_enough_for_the_endpoint() -> None:
    """RED before the fix: 22 chars, which the authorize page refuses."""
    from backend.executors.worker.claude_login import perform_claude_login_manual

    rec: dict[str, Any] = {}
    perform_claude_login_manual(
        read_input=lambda: "CODE",
        emit=lambda _m: None,
        exchanger=_fake_exchanger(rec),
        now_ms=lambda: 0,
    )
    assert len(rec["state"]) >= REAL_CLI_STATE_LEN, (
        f"state is {len(rec['state'])} chars; the authorize endpoint was measured to "
        f"reject anything shorter than the real CLI's {REAL_CLI_STATE_LEN}"
    )


def test_the_state_is_fresh_per_run() -> None:
    """Entropy is the point — a constant of the right LENGTH would pass the
    check above while destroying what state is for."""
    from backend.executors.worker.claude_login import perform_claude_login_manual

    seen = set()
    for _ in range(3):
        rec: dict[str, Any] = {}
        perform_claude_login_manual(
            read_input=lambda: "CODE",
            emit=lambda _m: None,
            exchanger=_fake_exchanger(rec),
            now_ms=lambda: 0,
        )
        seen.add(rec["state"])
    assert len(seen) == 3, seen
