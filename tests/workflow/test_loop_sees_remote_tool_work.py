"""The loop must see what an executor agent did over MCP — it happened in another process.

The agent loop runs in the WORKER. An executor agent acts through the MCP work tools, which
execute in the API container. Those calls never pass through the loop's ``_invoke_tool_safely``
— the only place the native path learns what was written — so the loop ends the run believing
the agent did nothing:

* ``written_paths == []`` → the verified Deliverable's ``artifact_refs`` come out **empty**
  (measured live, run 96dd7cfc: the agent wrote clamp.py + test_clamp.py, refs were ``[]``).
  That list is the PR/Slack changed-file list, the settle knowledge tags, the design→impl
  handoff seed, and the proof view's file WHITELIST — a file not in it is refused.
* it is also what the deliverable summary is composed from, which is why an executor run's
  summary degraded into the model's raw narration.
* ``registry.declared_contract is None`` → the verify-first gate looks undeclared, and the
  only reason verification ran at all was the E30 prose-parsed *synthetic* declare_verification
  (which T3 deletes).

The registry already exports its per-run state, and the MCP transport already persists it onto
the run. The loop simply never read it back. That is the whole fix.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from backend.workflow.application._drive_loop import _sync_remote_tool_state
from backend.workflow.application.tool_registry import WORK_TOOL_STATE_KEY
from backend.workflow.infrastructure.tools import ToolRegistry

pytestmark = pytest.mark.asyncio

_CONTRACT = {"checks": [{"kind": "shell", "command": "pytest -q"}]}


class _FakeSession:
    """Returns the run payload the MCP process committed, as a fresh read would."""

    def __init__(self, payload: dict | None) -> None:
        self._payload = payload
        self.reads = 0

    async def remote_work_state(self, _run_id: uuid.UUID) -> dict | None:
        self.reads += 1
        return (self._payload or {}).get(WORK_TOOL_STATE_KEY)


async def _sync(registry: ToolRegistry, written: list[str], payload: dict | None) -> None:
    """Drive the loop's sync step with a stand-in for the run row read."""
    await _sync_remote_tool_state(
        registry,
        written,
        state=(payload or {}).get(WORK_TOOL_STATE_KEY),
    )


async def test_the_loop_learns_what_the_agent_wrote_over_mcp(tmp_path: Path) -> None:
    registry = ToolRegistry(workspace_dir=tmp_path)
    written: list[str] = []
    payload = {
        WORK_TOOL_STATE_KEY: {
            "declared_contract": _CONTRACT,
            "written_paths": ["backend/common/clamp.py", "tests/common/test_clamp.py"],
        }
    }

    await _sync(registry, written, payload)

    assert written == ["backend/common/clamp.py", "tests/common/test_clamp.py"], (
        "the deliverable's artifact_refs come from this list — empty means a contentless "
        "deliverable and a proof view that serves nothing"
    )


async def test_the_loop_learns_the_contract_the_agent_declared_over_mcp(tmp_path: Path) -> None:
    """Without this the verify-first gate looks undeclared and only the E30 prose hack saved it."""
    registry = ToolRegistry(workspace_dir=tmp_path)

    await _sync(registry, [], {WORK_TOOL_STATE_KEY: {"declared_contract": _CONTRACT}})

    assert registry.declared_contract == _CONTRACT


async def test_the_sync_does_not_duplicate_paths(tmp_path: Path) -> None:
    """It runs every turn — a path already recorded must not be appended twice."""
    registry = ToolRegistry(workspace_dir=tmp_path)
    written = ["a.py"]

    await _sync(registry, written, {WORK_TOOL_STATE_KEY: {"written_paths": ["a.py", "b.py"]}})

    assert written == ["a.py", "b.py"]


async def test_the_native_path_is_untouched(tmp_path: Path) -> None:
    """A LiteLLM run persists no work-tool state — the sync must be a clean no-op."""
    registry = ToolRegistry(workspace_dir=tmp_path)
    written = ["written/by/the/loop.py"]

    await _sync(registry, written, None)

    assert written == ["written/by/the/loop.py"]
    assert registry.declared_contract is None, "restoring nothing must not unlock the gate"
