"""ask_user_question / emit_deliverable over MCP — the other half of the tool surface (T1b).

Parity audit #20: **only ``declare_verification`` can ever come back from an executor.**
Every other tool is unreachable, so an executor-driven run can never ask the founder a
blocking question and never emits a mid-run Deliver event — while the identical run on a
LiteLLM account can do both. These two tools touch no files, so moving file state
server-side (T1) does nothing for them: without this lift the asymmetry becomes permanent.

They are LOOP-owned pseudo-tools, not registry tools, so they cannot simply delegate:

* ``emit_deliverable`` is a side effect — persist a partial Deliverable + Deliver event.
* ``ask_user_question`` is CONTROL FLOW — it creates a Decision and the run PAUSES.

Which raises the question this module answers: when the loop lives inside the user's CLI,
who stops the work? Not the CLI. The tool records the Decision server-side; the loop reads
the run's pending Decision after the turn and terminates ``needs_decision``. We do not ask
the agent nicely to stop — we already measured, today, that a coding CLI trusts its own
tools over anything the prompt tells it.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from backend.mcp.api import McpPrincipal, ToolContext, ToolError, ToolRegistry
from backend.mcp.tools.work_tools import register_work_tools

pytestmark = pytest.mark.asyncio


def _principal(*, run_id: uuid.UUID | None, scopes: tuple[str, ...] = ("mcp:read", "mcp:write")):
    return McpPrincipal(
        user_id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        client_id="bsvibe-worker",
        scopes=frozenset(scopes),
        jti=uuid.uuid4(),
        run_id=run_id,
    )


async def _noop_persist(*_a: Any, **_k: Any) -> None:
    """Persistence is covered in test_work_tool_state_persists.py."""


class _FakeWork:
    sandbox = object()

    async def invoke(self, name: str, arguments: dict[str, Any]) -> str:
        return "ok"


@pytest.fixture
def registry() -> ToolRegistry:
    """Work tools with the two loop-owned effects injected — the same seam the composition
    root (``backend.api.main``) wires to ``create_decision`` / ``handle_emit_deliverable``."""
    asked: list[dict[str, Any]] = []
    delivered: list[dict[str, Any]] = []

    async def _ask(run_id: uuid.UUID, ctx: ToolContext, payload: dict[str, Any]) -> str:
        asked.append({"run_id": run_id, **payload})
        return "decision-created"

    async def _deliver(run_id: uuid.UUID, ctx: ToolContext, arguments: dict[str, Any]) -> str:
        delivered.append({"run_id": run_id, **arguments})
        return "deliverable-recorded"

    async def _registry_for_run(run_id: uuid.UUID, ctx: ToolContext) -> _FakeWork:
        return _FakeWork()

    reg = ToolRegistry()
    register_work_tools(
        reg,
        registry_for_run=_registry_for_run,
        record_question=_ask,
        record_deliverable=_deliver,
        persist_state=_noop_persist,
    )
    reg.asked = asked  # type: ignore[attr-defined]
    reg.delivered = delivered  # type: ignore[attr-defined]
    return reg


def _ctx(principal: McpPrincipal) -> ToolContext:
    return ToolContext(principal=principal, session=None)  # type: ignore[arg-type]


async def test_both_loop_owned_tools_are_exposed(registry: ToolRegistry) -> None:
    assert {"bsvibe_work_ask_user_question", "bsvibe_work_emit_deliverable"} <= set(
        registry.names()
    )


async def test_asking_the_founder_records_a_decision_against_the_run(
    registry: ToolRegistry,
) -> None:
    """The founder gets asked — from an executor-driven run, which today is impossible."""
    run_id = uuid.uuid4()

    out = await registry.call_tool(
        "bsvibe_work_ask_user_question",
        {"question": "Postgres or SQLite?", "options": ["Postgres", "SQLite"]},
        _ctx(_principal(run_id=run_id)),
    )

    asked = registry.asked  # type: ignore[attr-defined]
    assert asked[0]["run_id"] == run_id
    assert asked[0]["question"] == "Postgres or SQLite?"
    assert asked[0]["options"] == ["Postgres", "SQLite"]
    # The agent is told to stop. It is a courtesy — the loop terminates on the pending
    # Decision whether or not the CLI obeys.
    assert "stop" in out["result"].lower()


async def test_the_agent_is_told_to_stop_after_asking() -> None:
    """A courtesy, not the mechanism: the run pauses on the Decision whether or not the CLI
    obeys. A coding CLI trusts its own tools over anything the prompt says — measured."""
    from backend.mcp.tools.work_tools import _STOP_AFTER_ASKING

    assert "stop" in _STOP_AFTER_ASKING.lower()
    assert "paused" in _STOP_AFTER_ASKING.lower()


async def test_emitting_a_deliverable_records_it_against_the_run(registry: ToolRegistry) -> None:
    run_id = uuid.uuid4()

    await registry.call_tool(
        "bsvibe_work_emit_deliverable",
        {"artifact_type": "code", "summary": "Added the mean() helper"},
        _ctx(_principal(run_id=run_id)),
    )

    delivered = registry.delivered  # type: ignore[attr-defined]
    assert delivered[0]["run_id"] == run_id
    assert delivered[0]["artifact_type"] == "code"


async def test_both_need_a_run_scoped_token(registry: ToolRegistry) -> None:
    """Same invariant as every other work tool: the run comes from the token."""
    for tool, args in (
        ("bsvibe_work_ask_user_question", {"question": "?"}),
        ("bsvibe_work_emit_deliverable", {"artifact_type": "code", "summary": "x"}),
    ):
        with pytest.raises(ToolError, match="run"):
            await registry.call_tool(tool, args, _ctx(_principal(run_id=None)))


async def test_both_require_the_write_scope(registry: ToolRegistry) -> None:
    """Asking the founder and emitting a deliverable both CHANGE the run's state."""
    ro = _principal(run_id=uuid.uuid4(), scopes=("mcp:read",))
    for tool, args in (
        ("bsvibe_work_ask_user_question", {"question": "?"}),
        ("bsvibe_work_emit_deliverable", {"artifact_type": "code", "summary": "x"}),
    ):
        with pytest.raises(ToolError):
            await registry.call_tool(tool, args, _ctx(ro))
