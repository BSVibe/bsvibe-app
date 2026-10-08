"""#1077 — an agentic turn switches the CLI's built-ins OFF; it does not list them.

The agentic invocation denied Claude Code's built-ins BY NAME (``--disallowedTools
<list>``) because the wildcard ``--disallowedTools "*"`` also killed the MCP tools.
A vendor's list rots: CLI updates added ``TaskCreate``…, then ``ListAgents`` /
``ReportFindings`` / ``SendMessage``. The init guard (``_exposed_tools_are_ours``)
did its job and refused — and with a rotted list that meant EVERY agentic run
aborted. The BStockReport weekly report published nothing 08-17 → 08-31 (run
``cb37cbcd``). #814 added the three names, which only waits for the next release.

Measured 2026-10-08 on CLI 2.1.287 with a stdio MCP server attached:

    --tools ""                                     → init tools: ['mcp__probe__probe_echo']
    --tools "" --allowedTools mcp__probe__…        → init tools: ['mcp__probe__probe_echo']

``--tools ""`` ("Use \"\" to disable all tools" — the BUILT-IN set) removes every
built-in, present and future, and leaves MCP tools alone. The name list goes; the
init guard stays as the guarantee.
"""

from __future__ import annotations

import pytest

import backend.executors.worker.claude_code as cc
from tests.executors.worker._drain import drain
from tests.executors.worker.test_claude_code_remote_tools import (
    _TOOLS,
    _assistant_line,
    _ctx,
    _init_line,
    _patch,
    _Proc,
)

pytestmark = pytest.mark.asyncio


async def test_an_agentic_turn_switches_every_builtin_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _patch(monkeypatch, _Proc([_init_line(_TOOLS), _assistant_line("done")]))

    await drain(cc.ClaudeCodeExecutor().execute("build it", _ctx()))

    argv = calls[0]
    assert argv[argv.index("--tools") + 1] == ""


async def test_no_list_of_builtin_names_is_passed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A list is what rots — none is handed to the CLI any more."""
    calls = _patch(monkeypatch, _Proc([_init_line(_TOOLS), _assistant_line("done")]))

    await drain(cc.ClaudeCodeExecutor().execute("build it", _ctx()))

    assert "--disallowedTools" not in calls[0]
    assert not hasattr(cc, "_NATIVE_TOOLS")


async def test_the_init_guard_is_still_the_guarantee() -> None:
    """Positive control — a built-in the CLI exposes anyway still aborts the task."""
    event = {
        "type": "system",
        "subtype": "init",
        "tools": [*_TOOLS, "SomeToolInventedNextRelease"],
    }
    problem = cc._exposed_tools_are_ours(event, list(_TOOLS))
    assert problem is not None and "SomeToolInventedNextRelease" in problem
