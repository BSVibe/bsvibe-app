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


async def test_command_includes_skip_git_repo_check(monkeypatch: pytest.MonkeyPatch) -> None:
    # The worker runs each task in a fresh, EMPTY temp dir (never a git repo and
    # not in codex's trusted-projects list). Without --skip-git-repo-check the
    # CLI refuses with "Not inside a trusted directory" and writes nothing, so
    # the flag must always be passed.
    proc = _FakeProcess(stdout_lines=[_agent_message_line("x")])
    calls = _patch_subprocess(monkeypatch, proc)

    await _drain(CodexExecutor().execute("p", {"workspace_dir": "."}))

    assert "--skip-git-repo-check" in calls[0]


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


# ── Sanitized subprocess env (no parent Claude-Code session leakage) ──────────


async def test_subprocess_env_strips_session_markers_keeps_normal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "leak-me")
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("HOME", "/home/worker")

    envs: list[dict[str, str]] = []
    proc = _FakeProcess(stdout_lines=[_agent_message_line("x")])

    async def _fake_exec(*args: Any, **kwargs: Any) -> _FakeProcess:
        envs.append(dict(kwargs.get("env") or {}))
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)

    await _drain(CodexExecutor().execute("p", {}))

    env = envs[0]
    assert "CLAUDE_CODE_SESSION_ID" not in env
    assert "CLAUDECODE" not in env
    assert env["PATH"] == "/usr/bin:/bin"
    assert env["HOME"] == "/home/worker"


# ── Lift E15 — cancel propagation actually terminates the subprocess ────────


class _HangingProcess:
    """Fake subprocess that survives ``wait()`` until ``kill()`` is invoked.

    Mirrors :class:`tests.executors.worker.test_opencode._HangingProcess` —
    the codex CLI shim has the same dogfood-symptom shape (multi-turn agent
    loop that never exits without an explicit kill).
    """

    def __init__(self) -> None:
        self.stdin = _FakeStreamWriter()
        self.stdout = _FakeStreamReader([], hang=True)
        self.stderr = _FakeStreamReader([])
        self.returncode: int | None = None
        self.killed_at: float | None = None
        self.pid: int = 12345
        self._kill_event = asyncio.Event()

    async def wait(self) -> int:
        await self._kill_event.wait()
        if self.returncode is None:
            self.returncode = -9
        return self.returncode

    def kill(self) -> None:
        self.killed_at = asyncio.get_event_loop().time()
        self.returncode = -9
        self._kill_event.set()


async def test_cancel_kills_subprocess_promptly_no_wait_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lift E15 — codex parity of the opencode cancel test.

    When the wrapper Task is cancelled mid-stream the codex subprocess
    MUST be terminated quickly (kill called within a small bound), NOT
    after the inner ``finally`` blocks on ``process.wait()`` for the
    full per-task deadline.
    """
    proc = _HangingProcess()
    # _HangingProcess satisfies the fake-process interface structurally.
    _patch_subprocess(monkeypatch, proc)  # type: ignore[arg-type]

    from backend.executors.worker import codex as codex_mod

    def _fake_group_kill(p: Any) -> None:
        p.kill()

    monkeypatch.setattr(codex_mod, "_kill_process_group", _fake_group_kill)

    executor = CodexExecutor(timeout_seconds=3600)
    stream = executor.execute("long task", {"workspace_dir": "."})
    task = asyncio.create_task(_drain(stream))

    # Give the executor time to spawn and reach readline await.
    for _ in range(50):
        if task.done():
            break
        await asyncio.sleep(0.01)

    cancel_at = asyncio.get_event_loop().time()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2.0)

    assert proc.killed_at is not None, "subprocess.kill() must fire on cancel"
    elapsed = proc.killed_at - cancel_at
    assert elapsed < 0.5, f"kill must fire within 0.5s of cancel; got {elapsed:.3f}s"


async def test_subprocess_started_in_new_session_for_pgrp_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lift E15 — codex executor MUST spawn with ``start_new_session=True``."""
    proc = _FakeProcess(stdout_lines=[_agent_message_line("x")])
    spawn_kwargs: list[dict[str, Any]] = []

    async def _capture_exec(*args: Any, **kwargs: Any) -> _FakeProcess:
        spawn_kwargs.append(dict(kwargs))
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _capture_exec)
    await _drain(CodexExecutor().execute("p", {"workspace_dir": "."}))

    assert spawn_kwargs, "create_subprocess_exec was never called"
    assert spawn_kwargs[0].get("start_new_session") is True, (
        "codex executor must pass start_new_session=True"
    )


async def test_subprocess_uses_large_stream_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """``codex exec --json`` emits JSON lines that can exceed asyncio's default
    64 KiB StreamReader limit (full file contents / diffs / reasoning). Without
    a raised ``limit=``, ``readline()`` raises ``LimitOverrunError`` mid-run
    ("Separator is found, but chunk is longer than limit") and the task fails.
    The subprocess must be created with a large stream limit."""
    proc = _FakeProcess(stdout_lines=[_agent_message_line("x")])
    captured: dict[str, Any] = {}

    async def _fake_exec(*args: Any, **kwargs: Any) -> _FakeProcess:
        captured.update(kwargs)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
    await _drain(CodexExecutor().execute("p", {"system": "s", "model": "gpt-5"}))
    assert captured.get("limit", 0) >= 1024 * 1024, (
        "stream limit must be >= 1 MiB for large JSON lines"
    )
