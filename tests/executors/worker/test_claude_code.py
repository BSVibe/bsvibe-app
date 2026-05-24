"""Tests for the worker's ``claude_code`` subprocess executor (Lift 3).

The executor runs ``claude --print --output-format stream-json`` as an async
subprocess and parses its NDJSON stream into :class:`ExecutionChunk`s. NO real
``claude`` binary is ever invoked: every test monkeypatches
``asyncio.create_subprocess_exec`` with a fake process that emits canned NDJSON
lines (or a non-zero exit, bad JSON, or a hang), so the parse / done / error /
timeout / retry paths are proven deterministically.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from typing import Any

import pytest

from backend.executors.worker.claude_code import ClaudeCodeExecutor
from backend.executors.worker.executors import ExecutionChunk, collect

pytestmark = pytest.mark.asyncio


# ── A fake asyncio subprocess emitting canned stdout/stderr ──────────────────


class _FakeStreamReader:
    """Minimal ``asyncio.StreamReader`` stand-in over a list of byte lines."""

    def __init__(self, lines: Sequence[bytes], *, hang: bool = False) -> None:
        self._lines = list(lines)
        self._buf = b"".join(self._lines)
        self._hang = hang

    async def readline(self) -> bytes:
        if self._hang:
            # Never resolves — exercises the per-line read timeout path.
            await asyncio.sleep(3600)
        if not self._lines:
            return b""
        return self._lines.pop(0)

    async def read(self, n: int = -1) -> bytes:
        data, self._buf = self._buf, b""
        return data


class _FakeStreamWriter:
    def write(self, data: bytes) -> None:  # noqa: D401 - stdin sink
        return None

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        return None


class _FakeProcess:
    def __init__(
        self,
        *,
        stdout_lines: Sequence[bytes],
        stderr_lines: Sequence[bytes] = (),
        returncode: int = 0,
        hang_stdout: bool = False,
    ) -> None:
        self.stdin = _FakeStreamWriter()
        self.stdout = _FakeStreamReader(stdout_lines, hang=hang_stdout)
        self.stderr = _FakeStreamReader(stderr_lines)
        self._returncode = returncode
        self.returncode: int | None = None
        self._killed = False

    async def wait(self) -> int:
        self.returncode = self._returncode
        return self._returncode

    def kill(self) -> None:
        self._killed = True
        self.returncode = -9


def _patch_subprocess(monkeypatch: pytest.MonkeyPatch, proc: _FakeProcess) -> list[list[str]]:
    """Patch ``asyncio.create_subprocess_exec`` to return ``proc``.

    Returns a list that captures each invocation's argv for assertions.
    """
    calls: list[list[str]] = []

    async def _fake_exec(*args: Any, **kwargs: Any) -> _FakeProcess:
        calls.append([str(a) for a in args])
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
    return calls


def _assistant_line(text: str) -> bytes:
    event = {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}
    return (json.dumps(event) + "\n").encode("utf-8")


async def _drain(stream: AsyncIterator[ExecutionChunk]) -> list[ExecutionChunk]:
    return [c async for c in stream]


# ── Happy path: assistant deltas then a terminal done ────────────────────────


async def test_streams_assistant_deltas_then_done(monkeypatch: pytest.MonkeyPatch) -> None:
    proc = _FakeProcess(
        stdout_lines=[_assistant_line("Hello "), _assistant_line("world")],
        returncode=0,
    )
    _patch_subprocess(monkeypatch, proc)

    chunks = await _drain(ClaudeCodeExecutor().execute("do it", {"workspace_dir": "."}))

    deltas = [c.delta for c in chunks if c.delta]
    assert deltas == ["Hello ", "world"]
    assert chunks[-1].done is True
    assert chunks[-1].error is None


async def test_collect_aggregates_output(monkeypatch: pytest.MonkeyPatch) -> None:
    proc = _FakeProcess(stdout_lines=[_assistant_line("abc"), _assistant_line("def")])
    _patch_subprocess(monkeypatch, proc)

    result = await collect(ClaudeCodeExecutor().execute("p", {}))

    assert result.success is True
    assert result.stdout == "abcdef"
    assert result.error_message is None


async def test_command_includes_print_and_stream_json(monkeypatch: pytest.MonkeyPatch) -> None:
    proc = _FakeProcess(stdout_lines=[_assistant_line("x")])
    calls = _patch_subprocess(monkeypatch, proc)

    await _drain(ClaudeCodeExecutor().execute("p", {"system": "be brief", "model": "sonnet"}))

    argv = calls[0]
    assert "--print" in argv
    assert "--output-format" in argv
    assert "stream-json" in argv
    # system + model are forwarded as flags.
    assert "--append-system-prompt" in argv
    assert "be brief" in argv
    assert "--model" in argv
    assert "sonnet" in argv


# ── Failure paths ────────────────────────────────────────────────────────────


async def test_nonzero_exit_yields_error_chunk(monkeypatch: pytest.MonkeyPatch) -> None:
    proc = _FakeProcess(
        stdout_lines=[],
        stderr_lines=[b"boom\n"],
        returncode=2,
    )
    _patch_subprocess(monkeypatch, proc)

    chunks = await _drain(ClaudeCodeExecutor().execute("p", {}))

    assert chunks[-1].done is True
    assert chunks[-1].error is not None
    assert "boom" in chunks[-1].error


async def test_bad_json_lines_are_skipped_no_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    proc = _FakeProcess(
        stdout_lines=[b"not json\n", _assistant_line("ok"), b"{broken\n"],
        returncode=0,
    )
    _patch_subprocess(monkeypatch, proc)

    chunks = await _drain(ClaudeCodeExecutor().execute("p", {}))

    deltas = [c.delta for c in chunks if c.delta]
    assert deltas == ["ok"]
    assert chunks[-1].done is True
    assert chunks[-1].error is None


async def test_missing_binary_yields_error(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _raise(*args: Any, **kwargs: Any) -> Any:
        raise FileNotFoundError("claude not found")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _raise)

    chunks = await _drain(ClaudeCodeExecutor().execute("p", {}))

    assert chunks[-1].done is True
    assert chunks[-1].error is not None
    assert "claude not found" in chunks[-1].error


async def test_timeout_yields_error_chunk(monkeypatch: pytest.MonkeyPatch) -> None:
    # A subprocess whose stdout never produces a line — the executor's per-line
    # read deadline must fire and surface a terminal timeout error.
    proc = _FakeProcess(stdout_lines=[], hang_stdout=True)
    _patch_subprocess(monkeypatch, proc)

    executor = ClaudeCodeExecutor(timeout_seconds=0, total_timeout_seconds=0)
    chunks = await _drain(executor.execute("p", {}))

    assert chunks[-1].done is True
    assert chunks[-1].error is not None
    assert "timed out" in chunks[-1].error.lower()


# ── Rate-limit retry ─────────────────────────────────────────────────────────


async def test_rate_limit_retry_then_success(monkeypatch: pytest.MonkeyPatch) -> None:
    # First attempt: rate-limited (non-zero + "rate limit" in stderr).
    # Second attempt: succeeds. The retry sleep is patched out.
    attempts: list[_FakeProcess] = [
        _FakeProcess(stdout_lines=[], stderr_lines=[b"rate limit exceeded\n"], returncode=1),
        _FakeProcess(stdout_lines=[_assistant_line("recovered")], returncode=0),
    ]
    it = iter(attempts)

    async def _fake_exec(*args: Any, **kwargs: Any) -> _FakeProcess:
        return next(it)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)

    slept: list[float] = []

    async def _no_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)

    executor = ClaudeCodeExecutor(rate_limit_retries=2, rate_limit_wait_seconds=60)
    chunks = await _drain(executor.execute("p", {}))

    deltas = [c.delta for c in chunks if c.delta]
    assert deltas == ["recovered"]
    assert chunks[-1].done is True
    assert chunks[-1].error is None
    assert slept == [60]  # one retry wait fired


async def test_rate_limit_exhausted_surfaces_terminal_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _make() -> _FakeProcess:
        return _FakeProcess(stdout_lines=[], stderr_lines=[b"rate limit\n"], returncode=1)

    async def _fake_exec(*args: Any, **kwargs: Any) -> _FakeProcess:
        return _make()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)

    async def _no_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)

    executor = ClaudeCodeExecutor(rate_limit_retries=1, rate_limit_wait_seconds=1)
    chunks = await _drain(executor.execute("p", {}))

    assert chunks[-1].done is True
    assert chunks[-1].error is not None


async def test_supported_task_types() -> None:
    assert "coding" in ClaudeCodeExecutor().supported_task_types()
