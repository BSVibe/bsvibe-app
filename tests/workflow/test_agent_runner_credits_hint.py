"""The system-error failure reason gains a credits/billing HINT when it looks
like an auth failure.

A model executor (a coding-agent CLI) can report a *billing* failure — the
provider being out of credits — as a generic ``401 Invalid authentication
credentials``. That sent a live debugging session chasing tokens for hours. We
can't tell the two apart from the CLI's string, so when a run fails with an
auth-shaped reason we APPEND a hint pointing at the provider balance, without
changing reasons that don't look auth-related.
"""

from backend.workflow.application.agent_runner import _with_credits_hint


def test_hint_appended_for_401_reason() -> None:
    reason = (
        "loop crashed: Failed to authenticate. API Error: 401 Invalid authentication credentials"
    )
    out = _with_credits_hint(reason)
    assert out.startswith(reason)
    assert "credit" in out.lower() or "balance" in out.lower()


def test_hint_appended_for_unauthorized_and_authenticate_variants() -> None:
    for reason in (
        "loop crashed: 401 Unauthorized",
        "model call failed: authentication credentials invalid",
        "executor error: Failed to authenticate",
    ):
        assert "balance" in _with_credits_hint(reason).lower()


def test_no_hint_for_non_auth_reason() -> None:
    for reason in (
        "sandbox unavailable: docker daemon not reachable",
        "loop crashed: ValueError: bad plan",
        "agent loop system error",
    ):
        assert _with_credits_hint(reason) == reason


def test_hint_added_only_once_idempotent() -> None:
    once = _with_credits_hint("loop crashed: 401 unauthorized")
    twice = _with_credits_hint(once)
    assert once == twice


def test_empty_reason_unchanged() -> None:
    assert _with_credits_hint("") == ""
