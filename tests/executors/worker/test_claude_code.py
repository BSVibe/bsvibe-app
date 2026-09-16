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
from backend.executors.worker.executors import ExecutionChunk
from tests.executors.worker._drain import drain

pytestmark = pytest.mark.asyncio


def _agent_ctx(**extra: Any) -> dict[str, Any]:
    """A VALID agent task — one that carries BSVibe's tools.

    Since the local agent run was deleted, this is the ONLY agent shape there is: an
    agentic task without an MCP surface is refused before the CLI starts. Tests that
    are about something else (stream parsing, timeouts, cancellation, env) need a
    shape that actually runs, and this is it.
    """
    ctx: dict[str, Any] = {
        "agentic": True,
        "mcp_config": json.dumps({"mcpServers": {"bsvibe": {"type": "http", "url": "https://x"}}}),
        "allowed_tools": ["mcp__bsvibe__bsvibe_work_file_read"],
    }
    ctx.update(extra)
    return ctx


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
        # A real ``asyncio.subprocess.Process`` always has one, and the turn's
        # progress markers (#965) log it so a hung turn can be tied to a pid in
        # ``ps``. The double needs it for the same reason ``_kill_process_group``
        # does.
        self.pid = 4242
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

    chunks = await _drain(ClaudeCodeExecutor().execute("do it", _agent_ctx(workspace_dir=".")))

    deltas = [c.delta for c in chunks if c.delta]
    assert deltas == ["Hello ", "world"]
    assert chunks[-1].done is True
    assert chunks[-1].error is None


async def test_drain_aggregates_output(monkeypatch: pytest.MonkeyPatch) -> None:
    proc = _FakeProcess(stdout_lines=[_assistant_line("abc"), _assistant_line("def")])
    _patch_subprocess(monkeypatch, proc)

    result = await drain(ClaudeCodeExecutor().execute("p", _agent_ctx()))

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
    result = await drain(ClaudeCodeExecutor(rate_limit_retries=0).execute("p", _agent_ctx()))

    assert result.success is False
    assert "rate limit" in (result.error_message or "").lower()


async def test_allowed_rate_limit_event_with_exit_is_plain_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ``allowed`` rate_limit_event is benign — a non-zero exit alongside it
    is a normal failure, NOT classified as rate-limited (no spurious retries)."""
    proc = _FakeProcess(stdout_lines=[_rate_limit_line("allowed")], returncode=1)
    _patch_subprocess(monkeypatch, proc)

    result = await drain(ClaudeCodeExecutor(rate_limit_retries=0).execute("p", _agent_ctx()))

    assert result.success is False
    assert "rate limit" not in (result.error_message or "").lower()


async def test_command_includes_print_and_stream_json(monkeypatch: pytest.MonkeyPatch) -> None:
    proc = _FakeProcess(stdout_lines=[_assistant_line("x")])
    calls = _patch_subprocess(monkeypatch, proc)

    await _drain(ClaudeCodeExecutor().execute("p", _agent_ctx(system="be brief", model="sonnet")))

    argv = calls[0]
    assert "--print" in argv
    assert "--output-format" in argv
    assert "stream-json" in argv
    # system + model are forwarded as flags.
    assert "--append-system-prompt" in argv
    assert "be brief" in argv
    assert "--model" in argv
    assert "sonnet" in argv


async def test_the_agent_has_no_native_writes_to_confine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """This test used to assert that the agent's NATIVE file writes stay inside the
    per-task workspace: ``--dangerously-skip-permissions`` had disabled every guard
    including working-directory confinement, so an agent that learned the host
    source-repo path from the inherited memory wrote into that repo (dogfood leak).
    The fix then was ``--permission-mode acceptEdits`` — writes auto-apply, but only
    inside the cwd — plus Bash auto-allowed for the verify step.

    That whole shape is gone. The agent now acts ONLY through BSVibe's tools, so it
    has no native writes to confine and no native Bash to allow. The proposition is
    restated at the level that now carries it: the natives are DENIED, and neither
    the blanket bypass nor the confinement that stood in for it may reappear —
    ``acceptEdits`` coming back would mean native tools came back with it.
    """
    proc = _FakeProcess(stdout_lines=[_assistant_line("x")])
    calls = _patch_subprocess(monkeypatch, proc)

    await _drain(ClaudeCodeExecutor().execute("p", _agent_ctx()))

    argv = calls[0]
    assert "--dangerously-skip-permissions" not in argv
    assert "--permission-mode" not in argv
    assert "acceptEdits" not in argv
    # What replaced it: the CLI's own tools are taken away by name.
    assert "--disallowedTools" in argv
    assert "--strict-mcp-config" in argv


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
    # Nothing to permit: no edit mode, no Bash allow-list. ``--settings`` is present
    # now (it is how auto-memory is turned off, see below) so the proposition is stated
    # directly instead of through the absence of the flag: whatever it carries, it
    # grants nothing.
    assert "--permission-mode" not in argv
    assert "permissions" not in json.loads(argv[argv.index("--settings") + 1])


# ── TWO shapes, and there is no third ────────────────────────────────────────
#
# Founder's ruling (2026-09-16): the only executors that may exist are the MCP
# agent run and the chat turn. EVERY execution is BSVibe relaying between the LLM
# worker and the user — *including when the worker and the user's machine are the
# same box*. A run on the founder's own hardware is not a licence to hand the CLI
# that hardware.
#
# The local agent run was the third shape, and it was reached by ONE thing: an
# agentic task whose ``mcp_config`` was missing. It carried no ``--disallowedTools``
# (every native tool live), allowed Bash outright, and confined only WRITES to the
# cwd — so it could read the founder's whole filesystem.
#
# The producer was already strict: ``_work_tool_surface`` REFUSES a run-less agentic
# task rather than issue a workspace-wide token. Only the consumer was lenient, and
# that leniency is what built the unisolated path. So the absence is not handled —
# it is rejected, and the shape that allowed it is gone.


async def test_agentic_without_bsvibe_tools_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """No MCP surface → no agent run. NOT a degraded one: the CLI never starts."""
    proc = _FakeProcess(stdout_lines=[_assistant_line("x")])
    calls = _patch_subprocess(monkeypatch, proc)

    chunks = await _drain(ClaudeCodeExecutor().execute("p", {"system": "ctx", "agentic": True}))

    assert calls == [], "the CLI must not be launched at all"
    assert chunks[-1].done
    assert "bsvibe" in (chunks[-1].error or "").lower()


async def test_missing_agentic_key_is_refused_not_defaulted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A task from an older backend carries neither key. It used to DEFAULT to the
    agent run with native tools — the exact shape that is now forbidden. Version
    skew must fail loudly instead of running unisolated."""
    proc = _FakeProcess(stdout_lines=[_assistant_line("x")])
    calls = _patch_subprocess(monkeypatch, proc)

    chunks = await _drain(ClaudeCodeExecutor().execute("p", {}))

    assert calls == []
    assert chunks[-1].done and chunks[-1].error


async def test_chat_turn_needs_no_bsvibe_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    """The CONTROL: the refusal is aimed at agent runs, not at everything. A chat
    turn has nothing to reach, so it carries no MCP surface and still runs."""
    proc = _FakeProcess(stdout_lines=[_assistant_line("x")])
    calls = _patch_subprocess(monkeypatch, proc)

    chunks = await _drain(ClaudeCodeExecutor().execute("p", {"system": "ctx", "agentic": False}))

    assert len(calls) == 1
    assert not chunks[-1].error


async def test_agent_run_acts_only_through_bsvibe_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    """The surviving agent shape: BSVibe's tools, the CLI's own taken away."""
    proc = _FakeProcess(stdout_lines=[_assistant_line("x")])
    calls = _patch_subprocess(monkeypatch, proc)

    await _drain(
        ClaudeCodeExecutor().execute(
            "p",
            {
                "system": "ctx",
                "agentic": True,
                "mcp_config": {"mcpServers": {"bsvibe": {"url": "https://x"}}},
                "allowed_tools": ["mcp__bsvibe__bsvibe_work_file_read"],
            },
        )
    )

    argv = calls[0]
    assert "--strict-mcp-config" in argv
    assert "--disallowedTools" in argv
    # The deleted shape's fingerprints — neither may come back.
    assert "--permission-mode" not in argv
    assert "acceptEdits" not in argv
    # An agent run keeps the host harness prompt: OURS is APPENDED to it.
    assert "--append-system-prompt" in argv
    assert "--system-prompt" not in argv


# ── Failure paths ────────────────────────────────────────────────────────────


async def test_nonzero_exit_yields_error_chunk(monkeypatch: pytest.MonkeyPatch) -> None:
    proc = _FakeProcess(
        stdout_lines=[],
        stderr_lines=[b"boom\n"],
        returncode=2,
    )
    _patch_subprocess(monkeypatch, proc)

    chunks = await _drain(ClaudeCodeExecutor().execute("p", _agent_ctx()))

    assert chunks[-1].done is True
    assert chunks[-1].error is not None
    assert "boom" in chunks[-1].error


async def test_bad_json_lines_are_skipped_no_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    proc = _FakeProcess(
        stdout_lines=[b"not json\n", _assistant_line("ok"), b"{broken\n"],
        returncode=0,
    )
    _patch_subprocess(monkeypatch, proc)

    chunks = await _drain(ClaudeCodeExecutor().execute("p", _agent_ctx()))

    deltas = [c.delta for c in chunks if c.delta]
    assert deltas == ["ok"]
    assert chunks[-1].done is True
    assert chunks[-1].error is None


async def test_missing_binary_yields_error(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _raise(*args: Any, **kwargs: Any) -> Any:
        raise FileNotFoundError("claude not found")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _raise)

    chunks = await _drain(ClaudeCodeExecutor().execute("p", _agent_ctx()))

    assert chunks[-1].done is True
    assert chunks[-1].error is not None
    assert "claude not found" in chunks[-1].error


async def test_timeout_yields_error_chunk(monkeypatch: pytest.MonkeyPatch) -> None:
    # A subprocess whose stdout never produces a line — the executor's per-line
    # read deadline must fire and surface a terminal timeout error.
    proc = _FakeProcess(stdout_lines=[], hang_stdout=True)
    _patch_subprocess(monkeypatch, proc)

    executor = ClaudeCodeExecutor(timeout_seconds=0, total_timeout_seconds=0)
    chunks = await _drain(executor.execute("p", _agent_ctx()))

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
    chunks = await _drain(executor.execute("p", _agent_ctx()))

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
    chunks = await _drain(executor.execute("p", _agent_ctx()))

    assert chunks[-1].done is True
    assert chunks[-1].error is not None


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

    await _drain(ClaudeCodeExecutor().execute("p", _agent_ctx()))

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
    stream = executor.execute("long task", _agent_ctx(workspace_dir="."))
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
    await _drain(ClaudeCodeExecutor().execute("p", _agent_ctx(workspace_dir=".")))

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


# ── The agent must actually RECEIVE BSVibe's tools ───────────────────────────
# Everything below was measured against the real CLI (2.1.172) on 2026-07-14, driving the
# first real coding run through the T2b-4 path. Each assertion is a bug that run exposed.


async def test_subprocess_env_forces_a_BLOCKING_mcp_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Popping the var is NOT enough — the CLI's DEFAULT is non-blocking.

    With a non-blocking connect the CLI starts the turn before the (remote) MCP server has
    connected: ``system/init`` carries ``status: pending`` and ZERO tools. The agent then has
    no tools at all — natives denied, ours not yet there — and it does not say so. It emits
    fake tool calls as prose and fabricates a result ("The directory appears to be empty"),
    and the CLI reports success. Only ``=false`` makes the CLI wait (measured: status
    ``connected``, all 9 work tools in ``system/init``, real tool call, real result).
    """
    import backend.executors.worker.claude_code as cc

    monkeypatch.setattr(cc, "ensure_claude_bearer", lambda: None)
    monkeypatch.setenv("MCP_CONNECTION_NONBLOCKING", "true")  # the operator's shell sets this

    env = cc._subprocess_env_with_bearer()

    assert env.get("MCP_CONNECTION_NONBLOCKING") == "false", (
        "the worker must force a blocking MCP connect; unset != false (the CLI default is "
        "non-blocking, so the agent starts with no tools and fabricates)"
    )


async def test_native_denylist_covers_the_task_tool_family() -> None:
    """CLI 2.1.172 added TaskCreate/TaskGet/TaskList/TaskUpdate. The enumerated denylist rots;
    the guard below is the guarantee, but keep the list current so runs do not abort."""
    import backend.executors.worker.claude_code as cc

    denied = set(cc._NATIVE_TOOLS.split())

    assert {"TaskCreate", "TaskGet", "TaskList", "TaskUpdate"} <= denied


def _init(tools: list[str]) -> dict[str, object]:
    return {"type": "system", "subtype": "init", "tools": tools}


async def test_init_guard_aborts_when_OUR_TOOLS_ARE_ABSENT() -> None:
    """Absence is the dangerous case, and the guard used to miss it entirely.

    It only ever checked for EXCESS (``exposed - allowed``). An agent with nothing exposed
    passes that check — and then invents its answer. A loud abort beats a silent fabrication.
    """
    import backend.executors.worker.claude_code as cc

    allowed = ["mcp__bsvibe__bsvibe_work_file_read", "mcp__bsvibe__bsvibe_work_file_list"]

    abort = cc._unsanctioned_abort(_init([]), "{'mcpServers':{}}", allowed)

    assert abort is not None, "no tools at all must abort — the agent would fabricate"
    assert abort.done is True
    assert abort.error


async def test_init_guard_aborts_when_only_SOME_of_our_tools_arrived() -> None:
    import backend.executors.worker.claude_code as cc

    allowed = ["mcp__bsvibe__bsvibe_work_file_read", "mcp__bsvibe__bsvibe_work_file_list"]

    abort = cc._unsanctioned_abort(
        _init(["mcp__bsvibe__bsvibe_work_file_read"]), "{'mcpServers':{}}", allowed
    )

    assert abort is not None


async def test_init_guard_still_aborts_on_an_unsanctioned_native() -> None:
    import backend.executors.worker.claude_code as cc

    allowed = ["mcp__bsvibe__bsvibe_work_file_read"]

    abort = cc._unsanctioned_abort(
        _init(["mcp__bsvibe__bsvibe_work_file_read", "TaskCreate"]),
        "{'mcpServers':{}}",
        allowed,
    )

    assert abort is not None
    assert "TaskCreate" in (abort.error or "")


async def test_init_guard_passes_when_exactly_our_tools_are_exposed() -> None:
    import backend.executors.worker.claude_code as cc

    allowed = ["mcp__bsvibe__bsvibe_work_file_read", "mcp__bsvibe__bsvibe_work_file_list"]

    assert cc._unsanctioned_abort(_init(list(allowed)), "{'mcpServers':{}}", allowed) is None


# ── auto-memory: the CLI's per-cwd store is host state, and it IS injected ───
#
# ``--setting-sources ""`` stops the operator's CLAUDE.md and skills — MEASURED, and
# it really works: the host's 184 user skills load as 0. It does NOT stop the CLI's
# **auto-memory**, which is keyed by cwd, not by settings source. Measured against the
# real CLI (2026-09-16), one question — "do you have notes about BSVibe / Bot Fight
# Mode / a prod SHA? quote them" — asked four ways from a cwd whose auto-memory store
# is populated:
#
#   chat turn, flags as shipped                     → quoted the operator's private
#                                                     notes verbatim (prod SHAs, the
#                                                     security-gate status, infra)
#   chat turn + ``--settings autoMemoryEnabled``    → "NONE"
#   agentic turn, flags as shipped                  → quoted them verbatim
#   agentic turn + ``--settings autoMemoryEnabled`` → "NONE"
#
# Why this is not merely untidy: #973 collapsed every ``server_sandbox`` task into ONE
# fixed cwd. Auto-memory is keyed by cwd, so that is now ONE store shared by every task
# and every TENANT on the worker — and agent runs WRITE to it (the pre-#973 per-task dirs
# on this host hold agent-written memories, e.g. ``project_naive_datetime_utc_bug.md``).
# The per-task temp dir used to isolate these stores by accident; the cleanup merged them.
#
# ``--settings`` is honoured even under ``--setting-sources ""`` (measured), and unlike
# ``--bare`` it does not touch auth — ``--bare`` answered "Not logged in · Please run
# /login" (rc=1), and the worker is on the CLI-credential fallback right now.


def _auto_memory_off(argv: list[str]) -> bool:
    """True when this invocation turns the CLI's auto-memory store off."""
    if "--settings" not in argv:
        return False
    return json.loads(argv[argv.index("--settings") + 1]).get("autoMemoryEnabled") is False


async def test_chat_turn_does_not_inherit_host_auto_memory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A chat turn is a plain completion: the host's memory store is not its context."""
    proc = _FakeProcess(stdout_lines=[_assistant_line("x")])
    calls = _patch_subprocess(monkeypatch, proc)

    await _drain(ClaudeCodeExecutor().execute("p", {"system": "ctx", "agentic": False}))

    assert _auto_memory_off(calls[0])


async def test_mcp_agent_turn_does_not_inherit_host_auto_memory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The MCP agent path declares its own isolation (``--setting-sources ""``, natives
    denied, state reached only through BSVibe's tools). Auto-memory is host state that
    walked in behind that claim."""
    proc = _FakeProcess(stdout_lines=[_assistant_line("x")])
    calls = _patch_subprocess(monkeypatch, proc)

    await _drain(
        ClaudeCodeExecutor().execute(
            "p",
            {
                "system": "ctx",
                "agentic": True,
                "mcp_config": {"mcpServers": {"bsvibe": {"url": "https://x"}}},
                "allowed_tools": ["mcp__bsvibe__bsvibe_work_file_read"],
            },
        )
    )

    assert _auto_memory_off(calls[0])


async def test_no_executor_shape_inherits_the_host_auto_memory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#981 turned auto-memory off on the two shapes that DECLARE isolation and left
    the local agent run inheriting it, because its docstring called that inheritance
    deliberate. A control test pinned that difference.

    The founder's ruling deleted the local run outright, so the difference is gone
    and the control with it: there is no shape left that may inherit the store. That
    is asserted here directly — the old control could only have gone on passing by
    describing something that no longer exists.
    """
    proc = _FakeProcess(stdout_lines=[_assistant_line("x")])

    for ctx in ({"system": "ctx", "agentic": False}, _agent_ctx(system="ctx")):
        calls = _patch_subprocess(monkeypatch, proc)
        await _drain(ClaudeCodeExecutor().execute("p", ctx))
        assert _auto_memory_off(calls[0]), ctx
