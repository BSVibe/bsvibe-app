"""A failure that cannot succeed on retry must not be re-argued every 5 minutes (#970).

Measured on prod, 2026-09-17, one worker's log:

===========================  ==========  ==========================================
total lines                  1,817,107   264 MB in ONE file
rich traceback / box-drawing 1,752,344   **96.4% of the file**
``claude_oauth_refresh_failed``   6,164   ~284 rendered lines apiece
``claude_oauth_refresh_invalid_grant`` 6,163  **99.98% of those failures**
===========================  ==========  ==========================================

So the code already *knew*: ``_is_invalid_grant`` returned True, it logged the
one-line verdict — and then dumped a 284-line traceback anyway, unconditionally,
for something it had just classified as definitive. Nineteen days of that made
the real signal (silence after ``task_received``) the hardest thing in the file
to find, and it is what an investigation had to wade through first.

Two separate propositions are asserted here, because "less logging" is trivially
achieved by silencing diagnostics and that would be worse than the disease:

1. a **definitive** failure gets one line and no traceback;
2. an **unexpected** failure still gets its full traceback. This is the control.
   Without it, rung 1 passes just as well on a change that hides real errors.

And the retry cadence itself: a burned refresh token cannot be un-burned by
asking again 300 seconds later. Backing off is not merely quieter — it stops the
worker making a network call it has already been told the answer to. It must
still *recover on its own* once the credential is replaced, or the fix trades a
loud failure for a silent one.
"""

from __future__ import annotations

import asyncio
import io
import json
import urllib.error
from pathlib import Path
from typing import Any

import pytest

from backend.executors.worker import claude_auth

pytestmark = pytest.mark.asyncio

_NOW = 1_700_000_000_000
_HOUR = 3600 * 1000


class _CapturingLogger:
    """Records (event, kwargs) per level — the house pattern in test_main.py.

    ``structlog.testing.capture_logs`` is avoided deliberately: it cannot show
    whether ``exc_info`` was passed, and ``exc_info`` is the entire subject here.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def _record(self, level: str, event: str, **kw: Any) -> None:
        self.calls.append((level, event, kw))

    def debug(self, event: str, **kw: Any) -> None:
        self._record("debug", event, **kw)

    def info(self, event: str, **kw: Any) -> None:
        self._record("info", event, **kw)

    def warning(self, event: str, **kw: Any) -> None:
        self._record("warning", event, **kw)

    def error(self, event: str, **kw: Any) -> None:
        self._record("error", event, **kw)

    def exception(self, event: str, **kw: Any) -> None:
        self._record("exception", event, **kw)

    @property
    def events(self) -> list[str]:
        return [e for _, e, _ in self.calls]

    def with_traceback(self) -> list[str]:
        """Events that asked the renderer for a traceback — the expensive ones."""
        return [e for _, e, kw in self.calls if kw.get("exc_info")]


def _write_worker(path: Path, *, access: str, refresh: str, expires_at: int) -> None:
    path.write_text(
        json.dumps({"access_token": access, "refresh_token": refresh, "expires_at": expires_at})
    )


def _raise_invalid_grant(_rt: str) -> dict[str, Any]:
    """A *detectable* invalid_grant — 400 with a readable body.

    ⚠️ The pre-existing ``_invalid_grant`` helper in ``test_claude_auth.py``
    passes ``fp=None``, so ``exc.read()`` raises and ``_is_invalid_grant``
    returns **False**. It exercises the generic-failure path, not this one.
    """
    raise urllib.error.HTTPError(
        "https://example/oauth/token",
        400,
        "Bad Request",
        {},
        io.BytesIO(b'{"error":"invalid_grant","error_description":"refresh token revoked"}'),
    )


def _raise_unexpected(_rt: str) -> dict[str, Any]:
    raise OSError("connection reset by peer")


def _expired(tmp_path: Path) -> Path:
    p = tmp_path / "oauth.json"
    _write_worker(p, access="stale", refresh="burned", expires_at=_NOW - _HOUR)
    return p


def test_a_definitive_invalid_grant_costs_one_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cap = _CapturingLogger()
    monkeypatch.setattr(claude_auth, "logger", cap)

    claude_auth.ensure_claude_bearer(
        _expired(tmp_path),
        now_ms=_NOW,
        refresher=_raise_invalid_grant,
        cli_path=tmp_path / "absent.json",
    )

    assert "claude_oauth_refresh_invalid_grant" in cap.events, (
        "the verdict itself must still be logged — this is the line an operator greps for"
    )
    assert cap.with_traceback() == [], (
        "a failure the code has ALREADY classified as definitive does not need 284 "
        f"lines of stack to explain it; got {cap.with_traceback()}"
    )


def test_an_unexpected_failure_still_dumps_its_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control. Rung 1 alone is satisfied by a change that hides real errors."""
    cap = _CapturingLogger()
    monkeypatch.setattr(claude_auth, "logger", cap)

    claude_auth.ensure_claude_bearer(
        _expired(tmp_path),
        now_ms=_NOW,
        refresher=_raise_unexpected,
        cli_path=tmp_path / "absent.json",
    )

    assert "claude_oauth_refresh_failed" in cap.with_traceback(), (
        "an error nobody has classified is exactly the one worth a stack trace"
    )


# ---------------------------------------------------------------------------
# The outcome signal — the loop cannot back off on what it cannot see
# ---------------------------------------------------------------------------
def test_the_resolver_reports_invalid_grant_even_though_a_token_comes_back(
    tmp_path: Path,
) -> None:
    """``ensure_claude_bearer`` folds every outcome into ``str | None``.

    When the worker's own refresh is burned it falls back to the interactive
    CLI's credential and returns a perfectly good token — so the keep-alive
    logged ``claude_auth_keepalive_ok`` for nineteen days while the thing it
    exists to maintain was dead. A degraded result wearing the success shape
    cannot be backed off, alerted on, or counted.
    """
    cli = tmp_path / "cli.json"
    cli.write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "borrowed-from-cli",
                    "refreshToken": "cli-rt",
                    "expiresAt": _NOW + 8 * _HOUR,
                }
            }
        )
    )

    result = claude_auth.resolve_claude_bearer(
        _expired(tmp_path),
        now_ms=_NOW,
        refresher=_raise_invalid_grant,
        cli_path=cli,
    )

    assert result.token == "borrowed-from-cli", "the fallback must still keep work running"
    assert result.definitive_failure is True, (
        "the caller has to be able to tell 'refreshed' from 'borrowed while broken'"
    )


def test_a_healthy_refresh_is_not_a_definitive_failure(tmp_path: Path) -> None:
    p = tmp_path / "oauth.json"
    _write_worker(p, access="live", refresh="r0", expires_at=_NOW + 8 * _HOUR)

    result = claude_auth.resolve_claude_bearer(p, now_ms=_NOW, cli_path=tmp_path / "absent.json")

    assert result.token == "live"
    assert result.definitive_failure is False


def test_ensure_claude_bearer_still_returns_a_plain_token(tmp_path: Path) -> None:
    """Back-compat: every existing caller reads a token, not a result object."""
    p = tmp_path / "oauth.json"
    _write_worker(p, access="live", refresh="r0", expires_at=_NOW + 8 * _HOUR)

    assert claude_auth.ensure_claude_bearer(p, now_ms=_NOW, cli_path=tmp_path / "x") == "live"


# ---------------------------------------------------------------------------
# The cadence — stop asking a question that has been answered
# ---------------------------------------------------------------------------
async def _drive_keepalive(
    monkeypatch: pytest.MonkeyPatch,
    outcomes: list[Any],
    *,
    base_interval: float = 300.0,
) -> tuple[list[float], _CapturingLogger]:
    """Run the keep-alive for ``len(outcomes)`` ticks, recording each sleep."""
    from backend.executors.worker import main as worker_main

    cap = _CapturingLogger()
    monkeypatch.setattr(worker_main, "logger", cap)
    slept: list[float] = []
    stop = asyncio.Event()
    pending = list(outcomes)

    async def _fake_sleep(seconds: float, _stop: asyncio.Event) -> None:
        slept.append(seconds)
        if not pending:
            stop.set()

    monkeypatch.setattr(worker_main, "_interruptible_sleep", _fake_sleep)

    def _resolve() -> Any:
        return pending.pop(0)

    class _S:
        claude_auth_refresh_interval_s = base_interval

    await asyncio.wait_for(
        worker_main._claude_auth_keepalive_loop(settings=_S(), stop=stop, resolve=_resolve),
        timeout=5,
    )
    return slept, cap


class _Outcome:
    def __init__(self, token: str | None, definitive_failure: bool) -> None:
        self.token = token
        self.definitive_failure = definitive_failure


async def test_a_definitive_failure_backs_the_retry_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """300s × 6,164 attempts is what nineteen days of a burned token bought.

    Not one of them could have succeeded: the grant was revoked server-side.
    """
    burned = _Outcome("borrowed", definitive_failure=True)
    slept, _ = await _drive_keepalive(monkeypatch, [burned] * 4)

    assert slept[0] == 300.0, "the first retry is still prompt — the token may have just rotated"
    assert slept[1] > slept[0] and slept[2] > slept[1], (
        f"the interval must grow while the answer cannot change: {slept}"
    )


async def test_the_backoff_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unbounded doubling reaches days, and the worker would then take days to
    notice a credential the founder had already replaced."""
    burned = _Outcome("borrowed", definitive_failure=True)
    slept, _ = await _drive_keepalive(monkeypatch, [burned] * 12)

    assert max(slept) <= 3600.0, (
        f"backed off to {max(slept)}s — a replaced credential waits that long"
    )


async def test_recovery_resets_the_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """``bsvibe-worker claude-login`` writes a new credential. The worker must
    return to its normal cadence by itself, without a restart — otherwise the
    fix converts a loud failure into a silent one.

    ⚠️ Asserted on the **next failure**, not on the sleep right after recovery.
    A wire-cut proved the weaker version useless: leaving the accumulated
    backoff in place still sleeps the base interval on a healthy tick, so
    watching only that tick cannot tell a reset apart from no reset. What the
    reset actually buys is the PROMPT first retry the next time something
    breaks — measure that.
    """
    burned = _Outcome("borrowed", definitive_failure=True)
    healthy = _Outcome("fresh", definitive_failure=False)
    slept, _ = await _drive_keepalive(
        monkeypatch, [burned, burned, burned, healthy, burned, burned]
    )

    assert slept[:3] == [300.0, 600.0, 1200.0], f"unexpected ramp: {slept}"
    assert slept[3] == 300.0, f"a healthy tick must sleep the base interval: {slept}"
    assert slept[4] == 300.0, (
        f"the failure after a recovery started from the OLD backoff ({slept[4]}s) — "
        "a credential that breaks again would wait that long for its first retry"
    )
    assert slept[5] == 600.0, f"and the ramp must restart from the bottom: {slept}"


async def test_a_backed_off_tick_does_not_reprint_the_whole_story(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One line per attempt, and never a traceback from the loop itself."""
    burned = _Outcome("borrowed", definitive_failure=True)
    _, cap = await _drive_keepalive(monkeypatch, [burned] * 4)

    assert cap.with_traceback() == [], f"keep-alive dumped stacks: {cap.with_traceback()}"
    assert "claude_auth_keepalive_ok" not in cap.events, (
        "a borrowed CLI token is NOT the keep-alive working — that reading is what "
        "let a dead worker credential look healthy for nineteen days"
    )


# ---------------------------------------------------------------------------
# The renderer — why ONE traceback cost 284 lines
# ---------------------------------------------------------------------------
#
# The worker never called ``configure_logging``. structlog's *unconfigured*
# default is the development pipeline: ``ConsoleRenderer`` with rich exception
# formatting. So a daemon writing to a launchd-redirected file was emitting ANSI
# colour codes and box-drawing characters, and rendering every exception as a
# rich panel with source context and local variables.
#
# That is the multiplier behind the measurement at the top of this file. 6,164
# failures did not make 264 MB; 6,164 failures × ~284 rendered lines did. And it
# hurt twice: the volume, and the fact that the surviving signal was not
# greppable or parseable — an ANSI-coloured console dump is the one format you
# cannot `jq`.
#
# Configuring it is not a preference about looks. An unconfigured library runs on
# somebody else's default, and that default was chosen for a developer's terminal.


async def test_the_worker_daemon_configures_its_logging(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_amain`` must configure logging before anything can log.

    Both daemon entry points (``python -m backend.executors.worker`` and
    ``bsvibe-worker run``) converge here, so this is the one place that covers
    both. Asserted as "it was called, and with JSON" rather than by inspecting
    structlog's global state, which other tests in this suite mutate.
    """
    from backend.executors.worker import main as worker_main

    calls: list[dict[str, Any]] = []

    def _spy(**kw: Any) -> None:
        calls.append(kw)

    monkeypatch.setattr(worker_main, "configure_logging", _spy)

    # Stop immediately: we are asserting on startup, not on the poll loop.
    async def _noop(**_kw: Any) -> None:
        return None

    monkeypatch.setattr(worker_main, "poll_and_execute", _noop)
    monkeypatch.setattr(worker_main, "_connect_redis", lambda _s: None)
    monkeypatch.setattr(worker_main, "_ensure_process_group", lambda: None)

    await asyncio.wait_for(worker_main._amain(), timeout=10)

    assert calls, (
        "the daemon logged on structlog's UNCONFIGURED default — a developer "
        "console renderer, which is where the 284-line rich tracebacks came from"
    )
    assert calls[0].get("json_output") is not False, (
        f"a daemon's log must be machine-readable, got {calls[0]}"
    )
