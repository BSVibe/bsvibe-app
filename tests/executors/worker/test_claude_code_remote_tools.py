"""claude_code acts through BSVibe's tools — and we VERIFY it, not assume it (T2b-4).

The executor is the user's LLM client. An agentic turn must therefore reach the run's state
through BSVibe's MCP tools (server-side worktree + sandbox), with the CLI's own local tools
taken away.

Two measured constraints shape the invocation:

* ``--disallowedTools "*"`` — the clean wildcard — **kills MCP tools too** (a run with an MCP
  server attached reports ``NO_MCP_TOOLS``), and ``--allowedTools`` does not override it. So
  the natives must be denied **by name**.
* An enumerated denylist over a vendor's built-ins is exactly the trap this codebase already
  fell into today: my first list missed ``ToolSearch`` / ``Skill`` / ``Workflow``, and the
  agent burned twelve turns calling ``ToolSearch``. A new built-in in the next CLI release
  would silently hand the agent its local filesystem back.

So the list is best-effort and the CORRECTNESS is verified at runtime: the CLI's own
``system/init`` event announces the tools it actually exposed. If anything other than
BSVibe's tools is in it, the task ABORTS. We do not trust the flags; we check the outcome.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from backend.executors.worker.claude_code import ClaudeCodeExecutor
from backend.executors.worker.executors import collect

pytestmark = pytest.mark.asyncio

_MCP = {"mcpServers": {"bsvibe": {"type": "http", "url": "https://api.bsvibe.dev/mcp"}}}
_TOOLS = ["mcp__bsvibe__bsvibe_work_file_read", "mcp__bsvibe__bsvibe_work_file_write"]


def _ctx(**over: Any) -> dict[str, Any]:
    ctx: dict[str, Any] = {
        "system": "do the work",
        "agentic": True,
        "mcp_config": json.dumps(_MCP),
        "allowed_tools": _TOOLS,
    }
    ctx.update(over)
    return ctx


def _init_line(tools: list[str]) -> bytes:
    event = {"type": "system", "subtype": "init", "tools": tools, "mcp_servers": []}
    return (json.dumps(event) + "\n").encode()


def _assistant_line(text: str) -> bytes:
    event = {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}
    return (json.dumps(event) + "\n").encode()


class _Proc:
    def __init__(self, lines: list[bytes]) -> None:
        self.stdin = _Writer()
        self.stdout = _Reader(lines)
        self.stderr = _Reader([])
        self.returncode: int | None = None
        self.killed = False
        self.pid = 4242  # _kill_process_group group-kills by pid

    async def wait(self) -> int:
        self.returncode = 0
        return 0

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


class _Reader:
    def __init__(self, lines: list[bytes]) -> None:
        self._lines = list(lines)

    async def readline(self) -> bytes:
        return self._lines.pop(0) if self._lines else b""

    async def read(self, _n: int = -1) -> bytes:
        return b""


class _Writer:
    def write(self, _d: bytes) -> None: ...
    async def drain(self) -> None: ...
    def close(self) -> None: ...


def _patch(monkeypatch: pytest.MonkeyPatch, proc: _Proc) -> list[list[str]]:
    calls: list[list[str]] = []

    async def _exec(*args: Any, **_kw: Any) -> _Proc:
        calls.append([str(a) for a in args])
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _exec)
    return calls


# ── the invocation ──────────────────────────────────────────────────────────


async def test_the_cli_is_given_bsvibes_tools_and_stripped_of_its_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _patch(monkeypatch, _Proc([_init_line(_TOOLS), _assistant_line("done")]))

    await collect(ClaudeCodeExecutor().execute("build it", _ctx()))

    argv = calls[0]
    # BSVibe's tools, over MCP, with a run-scoped token in the config.
    assert argv[argv.index("--mcp-config") + 1] == json.dumps(_MCP)
    assert "--strict-mcp-config" in argv
    assert argv[argv.index("--allowedTools") + 1] == " ".join(_TOOLS)
    # Its own hands, taken away. The wildcard is unusable here — it kills MCP tools too — so
    # the natives are denied by name, and the init check below is what makes that safe.
    denied = argv[argv.index("--disallowedTools") + 1]
    for native in ("Bash", "Read", "Write", "Edit", "Glob", "Grep", "Task", "ToolSearch"):
        assert native in denied
    # No local edit permissions: there is nothing local left to permit.
    assert "--permission-mode" not in argv
    # The host operator's harness (CLAUDE.md, skills, their own MCP servers) is not the
    # agent's — it belongs to the founder's laptop, not to this run.
    assert argv[argv.index("--setting-sources") + 1] == ""


# ── the self-verification: do not trust the flags, check the outcome ────────


async def test_a_leaked_native_tool_aborts_the_task(monkeypatch: pytest.MonkeyPatch) -> None:
    """The CLI announced a tool we did not sanction — a new built-in in a CLI upgrade, say.

    That means the agent has hands we did not give it, and can reach the user's filesystem.
    The task fails loudly instead of running with them (the enumerated denylist is best
    effort; THIS is the guarantee)."""
    from backend.executors.worker import claude_code as cc

    proc = _Proc([_init_line([*_TOOLS, "Bash"]), _assistant_line("...")])
    _patch(monkeypatch, proc)
    killed: list[Any] = []
    # The real helper SIGKILLs the CLI's whole process group; here we only need to know it
    # was asked to. Reporting the leak while letting the agent keep working would be worse
    # than useless.
    monkeypatch.setattr(cc, "_kill_process_group", lambda p: killed.append(p))

    result = await collect(ClaudeCodeExecutor().execute("build it", _ctx()))

    assert result.success is False
    assert "Bash" in (result.error_message or "")
    assert killed == [proc], "the CLI must be stopped, not merely reported on"


async def test_exactly_our_tools_is_fine(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, _Proc([_init_line(_TOOLS), _assistant_line("ok")]))

    result = await collect(ClaudeCodeExecutor().execute("build it", _ctx()))

    assert result.success is True


async def test_no_tools_at_all_is_fine(monkeypatch: pytest.MonkeyPatch) -> None:
    """A chat turn exposes nothing — the empty set is a subset of ours."""
    _patch(monkeypatch, _Proc([_init_line([]), _assistant_line("42")]))

    result = await collect(
        ClaudeCodeExecutor().execute("what is 6*7?", {"agentic": False, "system": "s"})
    )

    assert result.success is True


# ── chat turns are unchanged ────────────────────────────────────────────────


async def test_a_chat_turn_gets_no_mcp_and_no_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch(monkeypatch, _Proc([_init_line([]), _assistant_line("42")]))

    await collect(ClaudeCodeExecutor().execute("q", {"agentic": False, "system": "ctx"}))

    argv = calls[0]
    assert argv[argv.index("--disallowedTools") + 1] == "*"  # chat: the wildcard IS usable
    assert "--mcp-config" not in argv or argv[argv.index("--mcp-config") + 1] == '{"mcpServers":{}}'
    assert "--allowedTools" not in argv
