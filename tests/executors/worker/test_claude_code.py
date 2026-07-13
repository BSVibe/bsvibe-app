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


def _rate_limit_line(status: str) -> bytes:
    event = {
        "type": "rate_limit_event",
        "rate_limit_info": {"status": status, "rateLimitType": "five_hour"},
    }
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


async def test_blocking_rate_limit_event_then_exit_is_rate_limited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-``allowed`` rate_limit_event followed by a non-zero exit (the
    five_hour-window-with-no-overage shape that exits 1 with empty stderr) is
    surfaced AS rate-limited — the worker's wait+retry path fires and the
    terminal error is actionable, not an opaque "claude exited"."""
    proc = _FakeProcess(stdout_lines=[_rate_limit_line("rejected")], returncode=1)
    _patch_subprocess(monkeypatch, proc)

    # retries=0 so the test asserts the classification without sleeping.
    result = await collect(ClaudeCodeExecutor(rate_limit_retries=0).execute("p", {}))

    assert result.success is False
    assert "rate limit" in (result.error_message or "").lower()


async def test_allowed_rate_limit_event_with_exit_is_plain_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ``allowed`` rate_limit_event is benign — a non-zero exit alongside it
    is a normal failure, NOT classified as rate-limited (no spurious retries)."""
    proc = _FakeProcess(stdout_lines=[_rate_limit_line("allowed")], returncode=1)
    _patch_subprocess(monkeypatch, proc)

    result = await collect(ClaudeCodeExecutor(rate_limit_retries=0).execute("p", {}))

    assert result.success is False
    assert "rate limit" not in (result.error_message or "").lower()


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


async def test_writes_are_confined_to_the_workspace(monkeypatch: pytest.MonkeyPatch) -> None:
    """The executor inherits the host operator's harness (CLAUDE.md / skills /
    memory) BY DESIGN, but the agent's native file writes must NOT escape the
    per-task workspace. ``--dangerously-skip-permissions`` disabled ALL guards
    including the working-directory confinement, so the agent — which learns the
    host source-repo path from the inherited memory — wrote into the host repo
    (dogfood leak). The fix: drop the bypass and confine writes to the cwd via
    ``--permission-mode acceptEdits`` (edits auto-apply, but only inside an
    allowed dir = the cwd) while still auto-allowing Bash for the verify step.
    """
    proc = _FakeProcess(stdout_lines=[_assistant_line("x")])
    calls = _patch_subprocess(monkeypatch, proc)

    await _drain(ClaudeCodeExecutor().execute("p", {}))

    argv = calls[0]
    # The blanket bypass is gone — it was what let writes escape the workspace.
    assert "--dangerously-skip-permissions" not in argv
    # Edits auto-apply headlessly but stay confined to the working directory.
    assert "--permission-mode" in argv
    assert "acceptEdits" in argv
    # Bash is auto-allowed (the verify step runs uv/pytest) without re-opening
    # file writes outside the workspace.
    settings_idx = argv.index("--settings")
    settings_blob = argv[settings_idx + 1]
    assert "Bash" in settings_blob


# ── chat parity: a chat turn is a plain completion, not an agent run ─────────
#
# BSVibe's first principle: an executor account and a LiteLLM account behave
# IDENTICALLY through the ``chat()`` abstraction. A LiteLLM call with no tools
# cannot inspect anything — it answers from the prompt. The executor must match.
#
# It did not. Every task, chat turns included, ran the full agentic CLI with tool
# access in an empty per-task temp dir — so a founder asking "현 프로젝트 상황
# 설명해줘" got an answer ABOUT THAT TEMP DIR ("완전히 비어 있는 임시 디렉토리입니다"),
# because the agent trusted its own tools over the injected grounding (prod,
# 2026-07-13). The same agentic boot is why async knowledge answers hit the 300 s
# executor timeout.
#
# ``agentic`` in the task context carries the ``tools`` argument's meaning down to
# the CLI: tools → agent run; no tools → completion, no tools, nothing to inspect.


async def test_chat_turn_runs_without_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    """agentic=False → a plain completion: no tools, no MCP, no host harness, and
    OUR system prompt — not Claude Code's (its default announces the cwd, so the
    model "knows" it sits in an empty temp dir even with every tool denied).

    Measured against the real CLI, same empty dir, same question:
      append-prompt + named denies → 12 turns, 44 s, answers about the temp dir
      this invocation             →  1 turn,   9 s, answers from the grounding
    """
    proc = _FakeProcess(stdout_lines=[_assistant_line("x")])
    calls = _patch_subprocess(monkeypatch, proc)

    await _drain(ClaudeCodeExecutor().execute("p", {"system": "ctx", "agentic": False}))

    argv = calls[0]
    # Wildcard, never an enumerated denylist: naming tools one by one left the CLI's
    # OTHER built-ins (ToolSearch, Skill, Workflow, Cron*, …) exposed, and the model
    # burned turns calling ToolSearch to go look at the project.
    assert argv[argv.index("--disallowedTools") + 1] == "*"
    # The host operator's MCP servers and settings (CLAUDE.md / skills) are the
    # agent's harness — a chat turn has no business inheriting them.
    assert "--strict-mcp-config" in argv
    assert argv[argv.index("--mcp-config") + 1] == '{"mcpServers":{}}'
    assert argv[argv.index("--setting-sources") + 1] == ""
    # REPLACE the system prompt, never append.
    assert argv[argv.index("--system-prompt") + 1] == "ctx"
    assert "--append-system-prompt" not in argv
    # Nothing to permit: no edit mode, no Bash allow-list.
    assert "--permission-mode" not in argv
    assert "--settings" not in argv


async def test_agent_run_keeps_its_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    """agentic=True → unchanged: the coding agent works in its sandbox."""
    proc = _FakeProcess(stdout_lines=[_assistant_line("x")])
    calls = _patch_subprocess(monkeypatch, proc)

    await _drain(ClaudeCodeExecutor().execute("p", {"system": "ctx", "agentic": True}))

    argv = calls[0]
    assert "--disallowedTools" not in argv
    assert "--strict-mcp-config" not in argv
    assert "--permission-mode" in argv
    assert "acceptEdits" in argv
    # An agent run keeps the host harness: the prompt is APPENDED to it.
    assert "--append-system-prompt" in argv
    assert "--system-prompt" not in argv


async def test_missing_agentic_defaults_to_agent_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """Back-compat: a task dispatched by an older backend carries no ``agentic``
    key. Default to the agent run — the coding loop must never silently lose its
    tools (that would ship empty diffs)."""
    proc = _FakeProcess(stdout_lines=[_assistant_line("x")])
    calls = _patch_subprocess(monkeypatch, proc)

    await _drain(ClaudeCodeExecutor().execute("p", {}))

    argv = calls[0]
    assert "--disallowedTools" not in argv
    assert "--permission-mode" in argv


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


# ── Sanitized subprocess env (no parent Claude-Code session leakage) ──────────


async def test_subprocess_env_strips_session_markers_keeps_normal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # When the worker is launched from inside a Claude Code session, the parent
    # env carries CLAUDE_CODE_* / CLAUDECODE markers that confuse a freshly
    # spawned ``claude``. The executor must pass a sanitized ``env=`` that drops
    # those markers but keeps normal env (PATH/HOME/ANTHROPIC_*).
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "leak-me")
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_AGENT_SDK_VERSION", "0.1.0")
    monkeypatch.setenv("CLAUDE_EFFORT", "high")
    monkeypatch.setenv("AI_AGENT", "claude-code")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("HOME", "/home/worker")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-keep")

    envs: list[dict[str, str]] = []
    proc = _FakeProcess(stdout_lines=[_assistant_line("x")])

    async def _fake_exec(*args: Any, **kwargs: Any) -> _FakeProcess:
        envs.append(dict(kwargs.get("env") or {}))
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)

    await _drain(ClaudeCodeExecutor().execute("p", {}))

    env = envs[0]
    assert "CLAUDE_CODE_SESSION_ID" not in env
    assert "CLAUDECODE" not in env
    assert "CLAUDE_AGENT_SDK_VERSION" not in env
    assert "CLAUDE_EFFORT" not in env
    assert "AI_AGENT" not in env
    assert env["PATH"] == "/usr/bin:/bin"
    assert env["HOME"] == "/home/worker"
    assert env["ANTHROPIC_API_KEY"] == "sk-keep"


# ── Lift E15 — cancel propagation actually terminates the subprocess ────────


class _HangingProcess:
    """Fake subprocess that survives ``wait()`` until ``kill()`` is invoked."""

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
    """Lift E15 — claude_code parity of the opencode cancel test.

    When the wrapper Task is cancelled mid-stream the ``claude`` subprocess
    MUST be terminated quickly, NOT after the inner ``finally`` blocks on
    ``process.wait()`` for the full per-task deadline.
    """
    proc = _HangingProcess()
    # _HangingProcess satisfies the fake-process interface structurally.
    _patch_subprocess(monkeypatch, proc)  # type: ignore[arg-type]

    from backend.executors.worker import claude_code as claude_mod

    def _fake_group_kill(p: Any) -> None:
        p.kill()

    monkeypatch.setattr(claude_mod, "_kill_process_group", _fake_group_kill)

    executor = ClaudeCodeExecutor(timeout_seconds=3600, total_timeout_seconds=7200)
    stream = executor.execute("long task", {"workspace_dir": "."})
    task = asyncio.create_task(_drain(stream))

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
    """Lift E15 — claude_code executor MUST spawn with ``start_new_session=True``."""
    proc = _FakeProcess(stdout_lines=[_assistant_line("x")])
    spawn_kwargs: list[dict[str, Any]] = []

    async def _capture_exec(*args: Any, **kwargs: Any) -> _FakeProcess:
        spawn_kwargs.append(dict(kwargs))
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _capture_exec)
    await _drain(ClaudeCodeExecutor().execute("p", {"workspace_dir": "."}))

    assert spawn_kwargs, "create_subprocess_exec was never called"
    assert spawn_kwargs[0].get("start_new_session") is True, (
        "claude_code executor must pass start_new_session=True"
    )


# ── Worker-managed OAuth bearer injection ────────────────────────────────────
# A launchd-spawned claude can't read the Keychain; the executor injects a
# worker-managed ANTHROPIC_AUTH_TOKEN (which the env sanitizer preserves) so it
# authenticates instead of falling back to a stale on-disk token → 401.


async def test_subprocess_env_injects_bearer_when_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backend.executors.worker.claude_code as cc

    monkeypatch.setattr(cc, "ensure_claude_bearer", lambda: "oat-live-token")
    env = cc._subprocess_env_with_bearer()
    assert env["ANTHROPIC_AUTH_TOKEN"] == "oat-live-token"


async def test_subprocess_env_omits_bearer_when_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backend.executors.worker.claude_code as cc

    monkeypatch.setattr(cc, "ensure_claude_bearer", lambda: None)
    env = cc._subprocess_env_with_bearer()
    assert "ANTHROPIC_AUTH_TOKEN" not in env
