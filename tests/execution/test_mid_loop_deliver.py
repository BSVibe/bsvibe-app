"""B12a — mid-loop Deliver events.

Workflow §1 (Deliver events are continuous side-emissions, NOT a single terminal
event) / §3.1 (one Deliver event = one external artifact). The agent loop must
be able to emit a Deliverable BEFORE reaching the verified terminal — each
``emit_deliverable`` tool call by the work LLM produces one Deliverable +
DeliveryEventRow (the side channel the Delivery Gateway drains).

This test pins:

* the new ``emit_deliverable`` tool surfaces in the loop's tool schema
* N mid-loop calls produce N Deliverable rows persisted before terminal
* the verified terminal still writes its CODE deliverable on top — additive,
  never duplicates

The terminal Deliverable is the SAME contract write_verified_deliverable
emits today; only the mid-loop additions are new (so the existing tests
remain green — back-compat regression below).
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.execution.db import (
    Deliverable,
    DeliverableType,
    ExecutionRun,
    RunStatus,
)
from backend.execution.orchestrator import (
    LoopToolCall,
    LoopTurn,
    RunOrchestrator,
)
from backend.supervisor.sandbox import NoopSandboxManager
from backend.workflow.infrastructure.delivery.db import DeliveryEventRow
from tests._support import memory_session

pytestmark = pytest.mark.asyncio


class ScriptedLlm:
    """Deterministic LLM stub — pops the next scripted turn."""

    def __init__(self, turns: list[LoopTurn]) -> None:
        self._turns = list(turns)
        self.calls: list[dict[str, Any]] = []

    async def complete(
        self, *, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None
    ) -> LoopTurn:
        self.calls.append({"messages": list(messages), "tools": tools})
        if not self._turns:
            raise AssertionError("ScriptedLlm exhausted — loop requested an unscripted turn")
        return self._turns.pop(0)


def _tc(name: str, **arguments: Any) -> LoopToolCall:
    return LoopToolCall(id=f"call-{name}-{uuid.uuid4().hex[:6]}", name=name, arguments=arguments)


def _declare_command(command: str) -> LoopToolCall:
    return _tc("declare_verification", checks=[{"kind": "command", "command": command}])


async def _make_run(session: AsyncSession, *, intent: str = "do the thing") -> ExecutionRun:
    run = ExecutionRun(
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        product_id=None,
        request_id=None,
        status=RunStatus.RUNNING,
        payload={"intent_text": intent},
    )
    session.add(run)
    await session.flush()
    return run


# --------------------------------------------------------------------------
# B12a — the new tool surfaces in the schema
# --------------------------------------------------------------------------


async def test_emit_deliverable_tool_advertised_in_loop_schema(tmp_path: Path) -> None:
    """``emit_deliverable`` must be in the tools schema the loop hands the LLM."""
    llm = ScriptedLlm(
        [
            LoopTurn(
                content="",
                tool_calls=(
                    _declare_command("test -f marker"),
                    _tc("file_write", path="marker", content="x"),
                ),
            ),
            LoopTurn(content="done", tool_calls=()),
        ]
    )
    async with memory_session() as session:
        run = await _make_run(session)
        orch = RunOrchestrator(session=session, llm=llm, sandbox_manager=NoopSandboxManager())
        await orch.run(run=run, workspace_dir=tmp_path)
    # First turn schema must advertise emit_deliverable.
    tools = llm.calls[0]["tools"]
    names = {(t.get("function") or {}).get("name") for t in tools}
    assert "emit_deliverable" in names


# --------------------------------------------------------------------------
# B12a — N mid-loop emits produce N Deliverable rows (+ the terminal one).
# --------------------------------------------------------------------------


async def test_n_mid_loop_emits_produce_n_deliverables(tmp_path: Path) -> None:
    """The agent emits TWO partial artifacts mid-loop (a PR + a Notion page),
    then declares + writes + the loop terminates verified. We expect:

      - 2 mid-loop Deliverables (artifact_type=pr, page) — emitted BEFORE the
        terminal — each with a DeliveryEventRow
      - 1 terminal Deliverable (artifact_type=code) — written at verified

    Total: 3 Deliverable rows, 3 DeliveryEventRow rows. The mid-loop ones must
    carry their artifact_type + summary in payload + a stable external_ref.
    """
    llm = ScriptedLlm(
        [
            LoopTurn(
                content="emitting partial artifacts",
                tool_calls=(
                    _tc(
                        "emit_deliverable",
                        artifact_type="pr",
                        summary="opened PR #15",
                        external_ref="github://acme/site/pull/15",
                        channel="github",
                    ),
                    _tc(
                        "emit_deliverable",
                        artifact_type="page",
                        summary="updated runbook page",
                        external_ref="notion://page/abc",
                        channel="notion",
                    ),
                    _declare_command("test -f marker"),
                    _tc("file_write", path="marker", content="x"),
                ),
            ),
            LoopTurn(content="done", tool_calls=()),
        ]
    )
    async with memory_session() as session:
        run = await _make_run(session)
        orch = RunOrchestrator(session=session, llm=llm, sandbox_manager=NoopSandboxManager())
        result = await orch.run(run=run, workspace_dir=tmp_path)
        assert result.outcome == "verified"

        deliverables = (
            (await session.execute(select(Deliverable).where(Deliverable.run_id == run.id)))
            .scalars()
            .all()
        )
        types = sorted(d.deliverable_type.value for d in deliverables)
        assert types == ["code", "page", "pr"], (
            f"expected 1 terminal CODE + 2 mid-loop emits, got {types}"
        )

        events = (
            (
                await session.execute(
                    select(DeliveryEventRow).where(
                        DeliveryEventRow.workspace_id == run.workspace_id
                    )
                )
            )
            .scalars()
            .all()
        )
        # 2 mid-loop deliver events + 1 terminal = 3.
        assert len(events) == 3, f"expected 3 DeliveryEventRows, got {len(events)}"
        event_types = sorted(e.artifact_type for e in events)
        assert event_types == ["code", "page", "pr"]

        # The mid-loop deliverables must carry their artifact metadata.
        pr_deliverable = next(d for d in deliverables if d.deliverable_type.value == "pr")
        assert pr_deliverable.payload.get("summary") == "opened PR #15"
        assert pr_deliverable.payload.get("external_ref") == "github://acme/site/pull/15"
        assert pr_deliverable.payload.get("channel") == "github"


# --------------------------------------------------------------------------
# B12a — emitting the SAME external_ref twice is idempotent (no dupes).
# --------------------------------------------------------------------------


async def test_emit_deliverable_idempotent_on_external_ref(tmp_path: Path) -> None:
    """Repeating an emit with the same external_ref must NOT create a second
    Deliverable — the LLM occasionally re-emits the same artifact across turns;
    the seam must be safe."""
    llm = ScriptedLlm(
        [
            LoopTurn(
                content="emit once",
                tool_calls=(
                    _tc(
                        "emit_deliverable",
                        artifact_type="pr",
                        summary="opened PR #99",
                        external_ref="github://acme/site/pull/99",
                    ),
                ),
            ),
            LoopTurn(
                content="emit same external_ref again — must dedupe",
                tool_calls=(
                    _tc(
                        "emit_deliverable",
                        artifact_type="pr",
                        summary="opened PR #99 again",
                        external_ref="github://acme/site/pull/99",
                    ),
                    _declare_command("test -f marker"),
                    _tc("file_write", path="marker", content="x"),
                ),
            ),
            LoopTurn(content="done", tool_calls=()),
        ]
    )
    async with memory_session() as session:
        run = await _make_run(session)
        orch = RunOrchestrator(session=session, llm=llm, sandbox_manager=NoopSandboxManager())
        result = await orch.run(run=run, workspace_dir=tmp_path)
        assert result.outcome == "verified"

        # Mid-loop emits: exactly one PR Deliverable (idempotent). Plus the
        # terminal CODE.
        deliverables = (
            (await session.execute(select(Deliverable).where(Deliverable.run_id == run.id)))
            .scalars()
            .all()
        )
        types = sorted(d.deliverable_type.value for d in deliverables)
        assert types == ["code", "pr"], f"expected dedupe → 1 PR + 1 CODE, got {types}"


# --------------------------------------------------------------------------
# Regression: a run that emits NOTHING mid-loop still works as today.
# --------------------------------------------------------------------------


async def test_run_without_mid_loop_emits_still_yields_one_terminal_deliverable(
    tmp_path: Path,
) -> None:
    """Back-compat: every test prior to B12a passes without emit_deliverable
    being called. The verified terminal still produces its CODE Deliverable
    on its own."""
    llm = ScriptedLlm(
        [
            LoopTurn(
                content="",
                tool_calls=(
                    _declare_command("test -f marker"),
                    _tc("file_write", path="marker", content="x"),
                ),
            ),
            LoopTurn(content="done", tool_calls=()),
        ]
    )
    async with memory_session() as session:
        run = await _make_run(session)
        orch = RunOrchestrator(session=session, llm=llm, sandbox_manager=NoopSandboxManager())
        result = await orch.run(run=run, workspace_dir=tmp_path)
        assert result.outcome == "verified"

        deliverables = (
            (await session.execute(select(Deliverable).where(Deliverable.run_id == run.id)))
            .scalars()
            .all()
        )
        assert len(deliverables) == 1
        assert deliverables[0].deliverable_type is DeliverableType.CODE
