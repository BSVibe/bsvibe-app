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
    # Without a system prompt the executor passes through the parent env
    # unchanged (no OPENCODE_CONFIG_CONTENT injected for this run).
    proc = _FakeProcess(stdout_lines=[_text_line("x")])
    _, envs = _patch_subprocess(monkeypatch, proc)

    await _drain(OpenCodeExecutor().execute("p", {}))

    assert "OPENCODE_CONFIG_CONTENT" not in envs[0] or not os.environ.get("OPENCODE_CONFIG_CONTENT")


async def test_supported_task_types() -> None:
    assert OpenCodeExecutor().supported_task_types() == ["opencode"]
