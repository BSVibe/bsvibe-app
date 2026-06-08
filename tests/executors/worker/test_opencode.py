"""Tests for the worker's ``opencode`` subprocess executor (Lift 3b).

The executor runs ``opencode run --format json`` as an async subprocess and
parses its flat JSONL stream into :class:`ExecutionChunk`s. NO real ``opencode``
binary is ever invoked: every test monkeypatches
``asyncio.create_subprocess_exec`` with a fake process that emits canned JSON
lines (or a non-zero exit, bad JSON, or a hang), so the parse / done / error /
timeout paths are proven deterministically.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator, Sequence
from typing import Any

import pytest

from backend.executors.worker.executors import ExecutionChunk, collect
from backend.executors.worker.opencode import OpenCodeExecutor

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


def _patch_subprocess(
    monkeypatch: pytest.MonkeyPatch, proc: _FakeProcess
) -> tuple[list[list[str]], list[dict[str, str]]]:
    """Patch ``asyncio.create_subprocess_exec`` to return ``proc``.

    Returns ``(argvs, envs)`` capturing each invocation's argv + env.
    """
    calls: list[list[str]] = []
    envs: list[dict[str, str]] = []

    async def _fake_exec(*args: Any, **kwargs: Any) -> _FakeProcess:
        calls.append([str(a) for a in args])
        envs.append(dict(kwargs.get("env") or {}))
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
    return calls, envs


def _text_line(text: str) -> bytes:
    """A ``text`` event — opencode's assistant-text record."""
    event = {"type": "text", "part": {"type": "text", "text": text}}
    return (json.dumps(event) + "\n").encode("utf-8")


async def _drain(stream: AsyncIterator[ExecutionChunk]) -> list[ExecutionChunk]:
    return [c async for c in stream]


# ── Happy path: text deltas then a terminal done ─────────────────────────────


async def test_streams_text_then_done(monkeypatch: pytest.MonkeyPatch) -> None:
    proc = _FakeProcess(
        stdout_lines=[
            (json.dumps({"type": "step_start"}) + "\n").encode("utf-8"),
            _text_line("Hello "),
            _text_line("world"),
            (json.dumps({"type": "step_finish", "part": {"reason": "stop"}}) + "\n").encode(
                "utf-8"
            ),
        ],
        returncode=0,
    )
    _patch_subprocess(monkeypatch, proc)

    chunks = await _drain(OpenCodeExecutor().execute("do it", {"workspace_dir": "."}))

    deltas = [c.delta for c in chunks if c.delta]
    assert deltas == ["Hello ", "world"]
    assert chunks[-1].done is True
    assert chunks[-1].error is None


async def test_collect_aggregates_output(monkeypatch: pytest.MonkeyPatch) -> None:
    proc = _FakeProcess(stdout_lines=[_text_line("abc"), _text_line("def")])
    _patch_subprocess(monkeypatch, proc)

    result = await collect(OpenCodeExecutor().execute("p", {}))

    assert result.success is True
    assert result.stdout == "abcdef"
    assert result.error_message is None


async def test_command_includes_run_and_format_json(monkeypatch: pytest.MonkeyPatch) -> None:
    proc = _FakeProcess(stdout_lines=[_text_line("x")])
    calls, envs = _patch_subprocess(monkeypatch, proc)

    await _drain(
        OpenCodeExecutor().execute(
            "do the task",
            {"system": "be brief", "model": "anthropic/claude", "workspace_dir": "/w"},
        )
    )

    argv = calls[0]
    assert "run" in argv
    assert "--format" in argv
    assert "json" in argv
    # workspace forwarded via --dir.
    assert "--dir" in argv
    assert "/w" in argv
    # model forwarded.
    assert "-m" in argv
    assert "anthropic/claude" in argv
    # prompt is the trailing positional arg.
    assert argv[-1] == "do the task"
    # system is injected via the OPENCODE_CONFIG_CONTENT env var.
    assert "OPENCODE_CONFIG_CONTENT" in envs[0]
    config = json.loads(envs[0]["OPENCODE_CONFIG_CONTENT"])
    assert "instructions" in config


# ── Failure paths ────────────────────────────────────────────────────────────


async def test_nonzero_exit_yields_error_chunk(monkeypatch: pytest.MonkeyPatch) -> None:
    proc = _FakeProcess(stdout_lines=[], stderr_lines=[b"boom\n"], returncode=2)
    _patch_subprocess(monkeypatch, proc)

    chunks = await _drain(OpenCodeExecutor().execute("p", {}))

    assert chunks[-1].done is True
    assert chunks[-1].error is not None
    assert "boom" in chunks[-1].error


async def test_bad_json_lines_are_skipped_no_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    proc = _FakeProcess(
        stdout_lines=[b"not json\n", _text_line("ok"), b"{broken\n"],
        returncode=0,
    )
    _patch_subprocess(monkeypatch, proc)

    chunks = await _drain(OpenCodeExecutor().execute("p", {}))

    deltas = [c.delta for c in chunks if c.delta]
    assert deltas == ["ok"]
    assert chunks[-1].done is True
    assert chunks[-1].error is None


async def test_missing_binary_yields_error(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _raise(*args: Any, **kwargs: Any) -> Any:
        raise FileNotFoundError("opencode not found")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _raise)

    chunks = await _drain(OpenCodeExecutor().execute("p", {}))

    assert chunks[-1].done is True
    assert chunks[-1].error is not None
    assert "opencode not found" in chunks[-1].error


async def test_timeout_yields_explicit_timeout_message(monkeypatch: pytest.MonkeyPatch) -> None:
    # A subprocess whose stdout never produces a line — the per-line read
    # deadline must fire and surface a terminal timeout error (not an empty
    # OSError; TimeoutError subclasses OSError in 3.11).
    proc = _FakeProcess(stdout_lines=[], hang_stdout=True)
    _patch_subprocess(monkeypatch, proc)

    executor = OpenCodeExecutor(timeout_seconds=0)
    chunks = await _drain(executor.execute("p", {}))

    assert chunks[-1].done is True
    assert chunks[-1].error is not None
    assert "timed out" in chunks[-1].error.lower()


async def test_no_system_no_config_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # Without a system prompt the executor injects no OPENCODE_CONFIG_CONTENT.
    proc = _FakeProcess(stdout_lines=[_text_line("x")])
    _, envs = _patch_subprocess(monkeypatch, proc)

    await _drain(OpenCodeExecutor().execute("p", {}))

    assert "OPENCODE_CONFIG_CONTENT" not in envs[0] or not os.environ.get("OPENCODE_CONFIG_CONTENT")


async def test_subprocess_env_strips_session_markers_keeps_normal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The opencode env (which layers OPENCODE_CONFIG_CONTENT) must still drop
    # the parent Claude-Code session markers while keeping normal env.
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "leak-me")
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("HOME", "/home/worker")

    proc = _FakeProcess(stdout_lines=[_text_line("x")])
    _, envs = _patch_subprocess(monkeypatch, proc)

    await _drain(OpenCodeExecutor().execute("p", {"system": "be brief"}))

    env = envs[0]
    assert "CLAUDE_CODE_SESSION_ID" not in env
    assert "CLAUDECODE" not in env
    assert env["PATH"] == "/usr/bin:/bin"
    assert env["HOME"] == "/home/worker"
    # The opencode-specific config is still layered on.
    assert "OPENCODE_CONFIG_CONTENT" in env


async def test_supported_task_types() -> None:
    assert OpenCodeExecutor().supported_task_types() == ["opencode"]


# ── Lift E15 — cancel propagation actually terminates the subprocess ────────


class _HangingProcess:
    """Fake subprocess that survives ``wait()`` until ``kill()`` is invoked.

    Models the dogfood symptom: an ``opencode run`` shim that is happily
    running its multi-turn agent loop and will NEVER exit on its own. Only
    ``kill()`` terminates it. ``wait()`` blocks forever otherwise.

    Exposes a ``pid`` so :func:`backend.executors.worker.executors._kill_process_group`
    can call ``os.killpg(os.getpgid(pid), SIGKILL)`` against it (we
    monkeypatch ``_kill_process_group`` so the OS call never actually
    fires — the test only asserts that ``kill()`` was invoked).
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
        # Block until ``kill`` flips the event — exactly like a real Popen
        # whose child won't exit unless we signal it.
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
    """Lift E15 — when the wrapper Task running an ``opencode`` stream is
    cancelled mid-flight, the subprocess MUST be terminated promptly
    (kill called within a small bound). The dogfood failure was the
    inner ``finally`` block waiting up to the per-task deadline
    (``timeout_seconds`` default 3600s) for ``process.wait()`` to return
    naturally — because ``opencode run`` is happily running its multi-turn
    LLM loop and will never exit on its own. Cancel propagation was
    silently swallowed for 20+ minutes per task.

    The fix: catch ``CancelledError`` inside the chunk loop and kill the
    process BEFORE awaiting ``process.wait()``. After this fix, a cancel
    fires ``process.kill()`` within ~100ms in the test environment, not
    after the (default 3600s) per-task deadline.
    """
    proc = _HangingProcess()
    # _HangingProcess satisfies the fake-process interface structurally.
    _patch_subprocess(monkeypatch, proc)  # type: ignore[arg-type]
    # Stub the OS-level group kill so the test never actually signals a
    # real pgrp; the executor's group kill just invokes ``process.kill()``
    # on the fake, flipping its ``killed_at`` + ending its ``wait()``.
    from backend.executors.worker import opencode as opencode_mod

    def _fake_group_kill(p: Any) -> None:
        p.kill()

    monkeypatch.setattr(opencode_mod, "_kill_process_group", _fake_group_kill)

    # ``timeout_seconds=3600`` is the production default for opencode; the
    # bug only surfaces with a long deadline (a short deadline lets the
    # inner ``wait_for`` time out quickly and mask the issue).
    executor = OpenCodeExecutor(timeout_seconds=3600)
    stream = executor.execute("long task", {"workspace_dir": "."})

    # Drive the generator forward until it's awaiting on stdout.
    task = asyncio.create_task(_drain(stream))

    # Let the executor enter the chunk loop (await readline → blocks).
    # The fake stdout's ``hang=True`` makes ``readline`` await forever.
    for _ in range(50):
        if proc.stdout._hang:  # pragma: no cover — invariant of _HangingProcess
            await asyncio.sleep(0.01)
        if not task.done():
            await asyncio.sleep(0.01)
        else:
            break

    cancel_at = asyncio.get_event_loop().time()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2.0)

    assert proc.killed_at is not None, (
        "subprocess.kill() must be invoked when the wrapper Task is "
        "cancelled — the dogfood bug was the cancel sitting on "
        "process.wait() forever"
    )
    elapsed = proc.killed_at - cancel_at
    assert elapsed < 0.5, (
        f"subprocess.kill() must fire within 0.5s of cancel; got "
        f"{elapsed:.3f}s — the inner finally is blocking on process.wait()"
    )


async def test_subprocess_started_in_new_session_for_pgrp_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lift E15 — the executor MUST spawn the subprocess with
    ``start_new_session=True`` so the child process becomes its own
    process-group leader. Without it, ``opencode``'s child processes
    (the Bun/Node agent loop spawned by the CLI shim) survive a
    ``process.kill()`` on the direct child — orphaned, burning CPU,
    holding network/file handles. The dogfood ``ps aux`` showed exactly
    this: opencode subprocesses alive 20+ minutes after their parent
    worker daemon should have terminated them.

    With ``start_new_session=True`` we can later signal the WHOLE group
    via ``os.killpg`` so SIGTERM/SIGKILL nukes every descendant atomically.
    """
    proc = _FakeProcess(stdout_lines=[_text_line("x")])
    spawn_kwargs: list[dict[str, Any]] = []

    async def _capture_exec(*args: Any, **kwargs: Any) -> _FakeProcess:
        spawn_kwargs.append(dict(kwargs))
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _capture_exec)

    await _drain(OpenCodeExecutor().execute("p", {"workspace_dir": "."}))

    assert spawn_kwargs, "create_subprocess_exec was never called"
    assert spawn_kwargs[0].get("start_new_session") is True, (
        "executor must pass start_new_session=True so the subprocess is "
        "its own pgrp leader — without it, killing the direct child leaves "
        "grandchild processes (the agent-loop workers) as orphans"
    )
