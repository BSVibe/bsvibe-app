"""A hung executor turn must say WHERE it stalled (#965).

2026-09-15, prod: framing turns hang. Between ``task_received`` and the
timeout the worker logs **nothing at all**, so three very different failures
are indistinguishable from the outside:

1. the subprocess never started (env build / the OAuth flock / exec itself);
2. it started and the model never produced a first event (API or CLI side);
3. it streamed, then stalled part-way.

Chasing this once already cost a session: every controllable input was
eliminated by replay — the CLI alone (2.4s), the worker's own env builder
(2.1s), ``--model sonnet`` (2.6s), the hung task's own persisted ``prompt`` and
``system`` bytes (3.8s), and a simulated launchd-minimal env (2.3s) — and the
silence is what stopped the diagnosis, not the absence of hypotheses.

So the turn emits two markers with elapsed times. Their PRESENCE splits the
three cases in one grep, which is the whole point: if neither fires it is (1),
if only the spawn marker fires it is (2), if both fire and the turn still dies
it is (3).
"""

from __future__ import annotations

from typing import Any

import pytest

pytestmark = pytest.mark.asyncio


class _CapturingLog:
    """Records structlog-style calls made on the module logger."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def info(self, event: str, **kw: Any) -> None:
        self.events.append((event, kw))

    def warning(self, event: str, **kw: Any) -> None:
        self.events.append((event, kw))

    def exception(self, event: str, **kw: Any) -> None:
        self.events.append((event, kw))

    def debug(self, event: str, **kw: Any) -> None:
        self.events.append((event, kw))


async def _drive(monkeypatch: pytest.MonkeyPatch, *, stdout_lines: list[bytes]) -> _CapturingLog:
    """Run one ClaudeCodeExecutor turn against a fake subprocess."""
    from backend.executors.worker import claude_code

    log = _CapturingLog()
    monkeypatch.setattr(claude_code, "logger", log)
    monkeypatch.setattr(
        claude_code, "_subprocess_env_with_bearer", lambda: {"PATH": "/usr/bin", "HOME": "/tmp"}
    )

    class _Stdin:
        def write(self, _b: bytes) -> None: ...
        async def drain(self) -> None: ...
        def close(self) -> None: ...

    class _Reader:
        def __init__(self, lines: list[bytes]) -> None:
            self._lines = list(lines)

        async def readline(self) -> bytes:
            return self._lines.pop(0) if self._lines else b""

        async def read(self, _n: int = -1) -> bytes:
            return b""

    class _Proc:
        pid = 4242
        returncode = 0

        def __init__(self) -> None:
            self.stdin = _Stdin()
            self.stdout = _Reader(stdout_lines)
            self.stderr = _Reader([])

        async def wait(self) -> int:
            return 0

    async def _fake_exec(*_a: Any, **_kw: Any) -> _Proc:
        return _Proc()

    monkeypatch.setattr(claude_code.asyncio, "create_subprocess_exec", _fake_exec)

    executor = claude_code.ClaudeCodeExecutor()
    async for _chunk in executor.execute("hi", {"workspace_dir": "/tmp", "agentic": False}):
        pass
    return log


async def test_the_turn_reports_that_the_subprocess_started(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Marker 1 — separates 'never started' from 'started and said nothing'."""
    log = await _drive(monkeypatch, stdout_lines=[b'{"type":"result"}\n'])
    names = [e for e, _ in log.events]
    assert "executor_turn_started" in names, names


async def test_the_turn_reports_its_first_event_with_elapsed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Marker 2 — and it carries HOW LONG the first event took.

    A turn that dies with the start marker but no first-event marker is the
    prod shape: the CLI ran and produced nothing.
    """
    log = await _drive(
        monkeypatch,
        stdout_lines=[b'{"type":"system"}\n', b'{"type":"assistant"}\n', b'{"type":"result"}\n'],
    )
    first = [kw for name, kw in log.events if name == "executor_turn_first_event"]
    assert first, [e for e, _ in log.events]
    assert "elapsed_s" in first[0], first[0]
    # Exactly ONCE across three lines. A marker that re-fires per line makes
    # ``elapsed_s`` mean "time to the latest line", which is a different — and
    # useless — number for the question this exists to answer.
    assert len(first) == 1, f"first-event marker fired {len(first)}x"


async def test_a_silent_turn_logs_the_start_but_never_the_first_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The prod case itself: a CLI that exits having emitted nothing.

    This is the assertion that makes the pair diagnostic rather than decorative
    — the two markers must be able to DISAGREE.
    """
    log = await _drive(monkeypatch, stdout_lines=[])
    names = [e for e, _ in log.events]
    assert "executor_turn_started" in names, names
    assert "executor_turn_first_event" not in names, names
