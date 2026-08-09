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
from tests.executors.worker._drain import drain

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

    await drain(ClaudeCodeExecutor().execute("build it", _ctx()))

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

    result = await drain(ClaudeCodeExecutor().execute("build it", _ctx()))

    assert result.success is False
    assert "Bash" in (result.error_message or "")
    assert killed == [proc], "the CLI must be stopped, not merely reported on"


async def test_exactly_our_tools_is_fine(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, _Proc([_init_line(_TOOLS), _assistant_line("ok")]))

    result = await drain(ClaudeCodeExecutor().execute("build it", _ctx()))

    assert result.success is True


async def test_no_tools_at_all_is_fine(monkeypatch: pytest.MonkeyPatch) -> None:
    """A chat turn exposes nothing — the empty set is a subset of ours."""
    _patch(monkeypatch, _Proc([_init_line([]), _assistant_line("42")]))

    result = await drain(
        ClaudeCodeExecutor().execute("what is 6*7?", {"agentic": False, "system": "s"})
    )

    assert result.success is True


# ── chat turns are unchanged ────────────────────────────────────────────────


async def test_a_chat_turn_gets_no_mcp_and_no_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch(monkeypatch, _Proc([_init_line([]), _assistant_line("42")]))

    await drain(ClaudeCodeExecutor().execute("q", {"agentic": False, "system": "ctx"}))

    argv = calls[0]
    assert argv[argv.index("--disallowedTools") + 1] == "*"  # chat: the wildcard IS usable
    assert "--mcp-config" not in argv or argv[argv.index("--mcp-config") + 1] == '{"mcpServers":{}}'
    assert "--allowedTools" not in argv


# ── the third shape: native tools AND BSVibe's platform tools ───────────────
# #692 parity. A ``client_attach`` run acts on the FOUNDER's own tree, so the
# workspace half (file / shell) must stay with the CLI's native tools — that is
# the whole point of the model. But withholding MCP entirely also withheld the
# PLATFORM half (knowledge / asking the founder / emitting a deliverable), which
# has nothing to do with where the source lives. Measured consequence: such a run
# could not emit a Deliverable, so ``connector_dispatch`` had nothing to load and
# NOTHING was ever delivered out.
#
# So the worker gains one execution instruction — ``native_tools`` — and keeps
# holding no product state of its own.


async def test_native_tools_plus_our_platform_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch(
        monkeypatch, _Proc([_init_line([*_TOOLS, "Bash", "Edit"]), _assistant_line("k")])
    )

    await drain(ClaudeCodeExecutor().execute("build it", _ctx(native_tools=True)))

    argv = calls[0]
    # Our MCP server, and ONLY ours — the host operator's servers are not this run's.
    assert argv[argv.index("--mcp-config") + 1] == json.dumps(_MCP)
    assert "--strict-mcp-config" in argv
    assert argv[argv.index("--allowedTools") + 1] == " ".join(_TOOLS)
    # The CLI KEEPS its own hands: this run's work happens through them, in place.
    assert "--disallowedTools" not in argv
    # Edits auto-apply headlessly but stay confined to the cwd (the founder's dir).
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"


async def test_native_shape_does_not_abort_on_the_clis_own_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Native tools are EXPECTED here, so exposing them is not a leak.

    Under the exclusive contract this exact init event aborts the task — which is
    why the guard has to learn the mode rather than the mode quietly bypassing it.
    """
    _patch(
        monkeypatch, _Proc([_init_line([*_TOOLS, "Bash", "Read", "Edit"]), _assistant_line("k")])
    )

    result = await drain(ClaudeCodeExecutor().execute("build it", _ctx(native_tools=True)))

    assert result.success is True


async def test_native_shape_still_aborts_when_our_tools_never_arrived(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The half that must NEVER be relaxed.

    An agent with no tools does not report that it has none — it fabricates (the
    CLI raced its own MCP connect, the model got zero tools, invented a tool call
    in prose and answered "The directory appears to be empty", reported success).
    Presence is asserted in BOTH modes; only the extras are mode-dependent.
    """
    from backend.executors.worker import claude_code as cc

    proc = _Proc([_init_line(["Bash", "Read", "Edit"]), _assistant_line("...")])
    _patch(monkeypatch, proc)
    monkeypatch.setattr(cc, "_kill_process_group", lambda p: None)

    result = await drain(ClaudeCodeExecutor().execute("build it", _ctx(native_tools=True)))

    assert result.success is False
    assert "never arrived" in (result.error_message or "")


async def test_exclusive_shape_is_unchanged_when_the_flag_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No flag → today's behaviour, exactly. A task dispatched by an older backend
    must not silently gain the founder's filesystem."""
    calls = _patch(monkeypatch, _Proc([_init_line(_TOOLS), _assistant_line("ok")]))

    await drain(ClaudeCodeExecutor().execute("build it", _ctx()))

    assert "--disallowedTools" in calls[0]
