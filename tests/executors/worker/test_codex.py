"""Tests for the worker's ``codex`` subprocess executor (Lift 3b).

The executor runs ``codex exec --json`` as an async subprocess and parses its
item-based JSONL stream into :class:`ExecutionChunk`s. NO real ``codex`` binary
is ever invoked: every test monkeypatches ``asyncio.create_subprocess_exec``
with a fake process that emits canned JSON lines (or a non-zero exit, bad JSON,
or a hang), so the parse / done / error / timeout paths are proven
deterministically.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from typing import Any

import pytest

from backend.executors.worker.codex import CodexExecutor
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
            await asyncio.sleep(3600)
        if not self._lines:
            return b""
        return self._lines.pop(0)

    async def read(self, n: int = -1) -> bytes:
        data, self._buf = self._buf, b""
        return data


class _FakeStreamWriter:
    def write(self, data: bytes) -> None:
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


def _agent_message_line(text: str) -> bytes:
    """A completed ``agent_message`` item — codex's whole-answer event."""
    event = {"type": "item.completed", "item": {"type": "agent_message", "text": text}}
    return (json.dumps(event) + "\n").encode("utf-8")


async def _drain(stream: AsyncIterator[ExecutionChunk]) -> list[ExecutionChunk]:
    return [c async for c in stream]


# ── Happy path: agent_message delta then a terminal done ─────────────────────


async def test_streams_agent_message_then_done(monkeypatch: pytest.MonkeyPatch) -> None:
    proc = _FakeProcess(
        stdout_lines=[
            (json.dumps({"type": "thread.started"}) + "\n").encode("utf-8"),
            _agent_message_line("Hello world"),
            (json.dumps({"type": "turn.completed"}) + "\n").encode("utf-8"),
        ],
        returncode=0,
    )
    _patch_subprocess(monkeypatch, proc)

    chunks = await _drain(CodexExecutor().execute("do it", {"workspace_dir": "."}))

    deltas = [c.delta for c in chunks if c.delta]
    assert deltas == ["Hello world"]
    assert chunks[-1].done is True
    assert chunks[-1].error is None


async def test_collect_aggregates_output(monkeypatch: pytest.MonkeyPatch) -> None:
    proc = _FakeProcess(
        stdout_lines=[_agent_message_line("abc"), _agent_message_line("def")],
    )
    _patch_subprocess(monkeypatch, proc)

    result = await collect(CodexExecutor().execute("p", {}))

    assert result.success is True
    assert result.stdout == "abcdef"
    assert result.error_message is None


async def test_command_includes_exec_and_json(monkeypatch: pytest.MonkeyPatch) -> None:
    proc = _FakeProcess(stdout_lines=[_agent_message_line("x")])
    calls = _patch_subprocess(monkeypatch, proc)

    await _drain(CodexExecutor().execute("p", {"system": "be brief", "model": "gpt-5"}))

    argv = calls[0]
    assert "exec" in argv
    assert "--json" in argv
    # system is forwarded via --config model_instructions_file=<path>.
    assert any(a.startswith("model_instructions_file=") for a in argv)
    # model is forwarded.
    assert "--model" in argv
    assert "gpt-5" in argv


# ── Failure paths ────────────────────────────────────────────────────────────


async def test_nonzero_exit_yields_error_chunk(monkeypatch: pytest.MonkeyPatch) -> None:
    proc = _FakeProcess(stdout_lines=[], stderr_lines=[b"boom\n"], returncode=2)
    _patch_subprocess(monkeypatch, proc)

    chunks = await _drain(CodexExecutor().execute("p", {}))

    assert chunks[-1].done is True
    assert chunks[-1].error is not None
    assert "boom" in chunks[-1].error


async def test_bad_json_lines_are_skipped_no_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    proc = _FakeProcess(
        stdout_lines=[b"not json\n", _agent_message_line("ok"), b"{broken\n"],
        returncode=0,
    )
    _patch_subprocess(monkeypatch, proc)

    chunks = await _drain(CodexExecutor().execute("p", {}))

    deltas = [c.delta for c in chunks if c.delta]
    assert deltas == ["ok"]
    assert chunks[-1].done is True
    assert chunks[-1].error is None


async def test_missing_binary_yields_error(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _raise(*args: Any, **kwargs: Any) -> Any:
        raise FileNotFoundError("codex not found")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _raise)

    chunks = await _drain(CodexExecutor().execute("p", {}))

    assert chunks[-1].done is True
    assert chunks[-1].error is not None
    assert "codex not found" in chunks[-1].error


async def test_timeout_yields_explicit_timeout_message(monkeypatch: pytest.MonkeyPatch) -> None:
    # A subprocess whose stdout never produces a line — the per-line read
    # deadline must fire and surface a terminal timeout error (not an empty
    # OSError; TimeoutError subclasses OSError in 3.11).
    proc = _FakeProcess(stdout_lines=[], hang_stdout=True)
    _patch_subprocess(monkeypatch, proc)

    executor = CodexExecutor(timeout_seconds=0)
    chunks = await _drain(executor.execute("p", {}))

    assert chunks[-1].done is True
    assert chunks[-1].error is not None
    assert "timed out" in chunks[-1].error.lower()


async def test_supported_task_types() -> None:
    assert CodexExecutor().supported_task_types() == ["codex"]
