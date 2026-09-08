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
from pathlib import Path
from typing import Any

import pytest

from backend.executors.worker.claude_code import ClaudeCodeExecutor
from tests.executors.worker._drain import drain

pytestmark = pytest.mark.asyncio

#: The run-scoped token really does ride in this config — that is the whole point of it
#: (``build_work_tool_dispatch``). A fixture without a secret in it cannot tell whether the
#: secret leaks, so this one has one.
_TOKEN = "eyJhbGciOiJFUzI1NiJ9.a-run-scoped-secret.signature"
_MCP = {
    "mcpServers": {
        "bsvibe": {
            "type": "http",
            "url": "https://api.bsvibe.dev/mcp",
            "headers": {"Authorization": f"Bearer {_TOKEN}"},
        }
    }
}
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


#: What the launch stub saw at the moment of exec, for the file the CLI is told to read.
#: Captured there and not after, because that file is deleted when the task ends.
_seen_config: dict[str, Any] = {}


def _patch(monkeypatch: pytest.MonkeyPatch, proc: _Proc) -> list[list[str]]:
    calls: list[list[str]] = []
    _seen_config.clear()

    async def _exec(*args: Any, **_kw: Any) -> _Proc:
        argv = [str(a) for a in args]
        calls.append(argv)
        if "--mcp-config" in argv:
            path = Path(argv[argv.index("--mcp-config") + 1])
            if path.exists():
                _seen_config["path"] = path
                _seen_config["text"] = path.read_text(encoding="utf-8")
                _seen_config["mode"] = path.stat().st_mode & 0o777
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
    # BSVibe's tools, over MCP. The config reaches the CLI by PATH — see
    # ``test_the_run_scoped_token_never_reaches_the_command_line`` for why.
    assert _seen_config["text"] == json.dumps(_MCP)
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


# ── there is no third shape ──────────────────────────────────────────────────
# #692 gave ``client_attach`` a shape of its own: the CLI kept its NATIVE tools
# for the workspace half while BSVibe served only the platform half over MCP.
# 형님 판정 2026-08-24 removed it — a worker is not guaranteed to be the client,
# so an agent must never act with the CLI's own hands. WHERE a work tool runs is
# the sandbox's business (``ClientWorkerSandboxSession`` dispatches each command
# to the founder's machine); the SURFACE does not branch.


async def test_the_clis_own_tools_abort_the_task_in_every_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exemption is gone: this init event used to be accepted for client_attach.

    It is the excess half of the guard — hands BSVibe never sanctioned. Under the
    old relaxation the agent could reach the founder's filesystem directly, which
    is how a run once invented a codebase in an empty dir and shipped it.
    """
    from backend.executors.worker import claude_code as cc

    _patch(
        monkeypatch, _Proc([_init_line([*_TOOLS, "Bash", "Read", "Edit"]), _assistant_line("k")])
    )
    monkeypatch.setattr(cc, "_kill_process_group", lambda p: None)

    result = await drain(ClaudeCodeExecutor().execute("build it", _ctx()))

    assert result.success is False
    assert "unsanctioned" in (result.error_message or "")


async def test_the_absence_half_still_aborts(monkeypatch: pytest.MonkeyPatch) -> None:
    """The half that must NEVER be relaxed.

    An agent with no tools does not report that it has none — it fabricates (the
    CLI raced its own MCP connect, the model got zero tools, invented a tool call
    in prose and answered "The directory appears to be empty", reported success).
    """
    from backend.executors.worker import claude_code as cc

    _patch(monkeypatch, _Proc([_init_line(["Bash", "Read", "Edit"]), _assistant_line("...")]))
    monkeypatch.setattr(cc, "_kill_process_group", lambda p: None)

    result = await drain(ClaudeCodeExecutor().execute("build it", _ctx()))

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


# ── the token is a secret, and argv is not a secret ─────────────────────────


async def test_the_run_scoped_token_never_reaches_the_command_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--mcp-config`` carries the run's bearer token, and argv is world-readable.

    Anything running as this user on the worker host can read it out of ``ps`` for as long
    as the task runs — and an agent task is minutes, not milliseconds. The token is
    run-scoped with a 90-minute TTL, which bounds the blast radius but does not make it
    zero: within that window it is ``mcp:write`` on the run.

    The CLI takes ``--mcp-config`` as a FILE or a string (measured against the installed
    binary: *"Load MCP servers from JSON files or strings"*), so the fix costs nothing but
    a temp file — the same shape ``codex.py`` already uses for its system prompt."""
    calls = _patch(monkeypatch, _Proc([_init_line(_TOOLS), _assistant_line("done")]))

    await drain(ClaudeCodeExecutor().execute("build it", _ctx()))

    argv = calls[0]
    assert _TOKEN not in " ".join(argv), "the run-scoped token is visible in ps"
    assert not any("Bearer" in a for a in argv), f"an Authorization header on argv: {argv!r}"
    # And it really did get there — otherwise this test passes by the CLI losing its tools.
    assert _TOKEN in _seen_config["text"], "the config the CLI reads must still carry the token"


async def test_the_config_file_is_readable_only_by_this_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Moving a secret from argv to a file is only a fix if the file is not world-readable."""
    _patch(monkeypatch, _Proc([_init_line(_TOOLS), _assistant_line("done")]))

    await drain(ClaudeCodeExecutor().execute("build it", _ctx()))

    assert _seen_config["mode"] == 0o600, f"config file mode {_seen_config['mode']:o}"


async def test_the_config_file_is_gone_when_the_task_ends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A token left on disk after the run outlives the run. The worker hosts long-lived
    daemons, so "the process exits" is not the cleanup."""
    _patch(monkeypatch, _Proc([_init_line(_TOOLS), _assistant_line("done")]))

    await drain(ClaudeCodeExecutor().execute("build it", _ctx()))

    assert not _seen_config["path"].exists(), "the config file outlived the task"
