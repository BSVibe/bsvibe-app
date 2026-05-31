"""D6 — mid-loop Deliverables stream live + are distinguished from the verified-final.

Synthesis §13 / Workflow §1: Deliver is a continuous side channel — a long-running
agent should emit partial Deliverables AS THEY HAPPEN so the Fleet / Brief / Run
views stay glanceable in real time, not only on terminal land.

These tests pin the D6 deltas the existing B12a tests do NOT cover:

3. **SSE streaming**: each successful ``emit_deliverable`` tool call PUBLISHES a
   ``deliverable.partial`` :class:`LiveEvent` onto the workspace's
   :class:`LiveEventBus` BEFORE the verified terminal. A dedup re-emit does NOT
   re-publish.
6. **No double-deliver**: the verified-final Deliverable's payload is NEVER
   ``kind=mid_loop_partial`` — the verified terminal path does not also write a
   partial of the same artifact.
2. **Distinction in payload**: every mid-loop emit's Deliverable has
   ``payload["kind"] == "mid_loop_partial"``; the verified-final does NOT.

Delta 1 (count) + delta 5 (back-compat) live in
``tests/execution/test_mid_loop_deliver.py``. Delta 4 (Safe Mode per-emission)
lives in ``tests/glue/test_safe_mode_per_emission_partial.py``. Delta 2
(API/UI distinction) lives in ``tests/api/test_run_detail_partials.py``.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.v1.live_events import (
    EVENT_DELIVERABLE_PARTIAL,
    LiveEvent,
    LiveEventBus,
)
from backend.execution.db import (
    Deliverable,
    DeliverableType,
    ExecutionRun,
    RunStatus,
)
from backend.execution.verified_deliverable import PARTIAL_DELIVERABLE_KIND
from backend.supervisor.sandbox import NoopSandboxManager
from backend.workflow.application.agent_loop import LoopToolCall, LoopTurn, RunOrchestrator
from tests._support import memory_session

pytestmark = pytest.mark.asyncio


class ScriptedLlm:
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


async def _drain(queue: Any, *, deadline_s: float = 0.5) -> list[LiveEvent]:  # noqa: ASYNC109
    """Drain all currently-buffered events from a subscriber queue (bounded wait)."""
    import asyncio  # noqa: PLC0415 — test-only

    events: list[LiveEvent] = []
    while True:
        try:
            events.append(await asyncio.wait_for(queue.get(), timeout=deadline_s))
        except TimeoutError:
            return events


# ---------------------------------------------------------------------------
# Delta 3 — each emit publishes a deliverable.partial LiveEvent
# ---------------------------------------------------------------------------


async def test_mid_loop_emit_publishes_partial_live_event(tmp_path: Path) -> None:
    """An ``emit_deliverable`` tool call must publish a ``deliverable.partial``
    LiveEvent on the workspace bus so PWA Run-view consumers wake up as the
    partial lands — not only at the verified terminal.

    The bus is constructed test-local + injected into the orchestrator (via
    ``live_event_bus=`` constructor kwarg) so we don't mutate the process-wide
    singleton across tests. The orchestrator MUST honour an injected bus and
    fall back to the singleton when none is given.
    """
    bus = LiveEventBus()
    llm = ScriptedLlm(
        [
            LoopTurn(
                content="emitting two partials",
                tool_calls=(
                    _tc(
                        "emit_deliverable",
                        artifact_type="pr",
                        summary="opened PR #1",
                        external_ref="github://acme/site/pull/1",
                    ),
                    _tc(
                        "emit_deliverable",
                        artifact_type="page",
                        summary="updated notion page",
                        external_ref="notion://page/xyz",
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

        # Subscribe BEFORE running so we can capture publishes as they land.
        async with bus.subscribe(run.workspace_id) as queue:
            orch = RunOrchestrator(
                session=session,
                llm=llm,
                sandbox_manager=NoopSandboxManager(),
                live_event_bus=bus,
            )
            result = await orch.run(run=run, workspace_dir=tmp_path)
            assert result.outcome == "verified"
            events = await _drain(queue)

    partial_events = [e for e in events if e.event_type == EVENT_DELIVERABLE_PARTIAL]
    assert len(partial_events) == 2, (
        f"expected exactly 2 deliverable.partial events (one per emit), got {len(partial_events)}: "
        f"{[(e.event_type, e.data) for e in events]}"
    )
    # Each event carries the wake-up id payload (deliverable_id + run_id) — tiny,
    # IDs only, no LLM content (B16 wire contract).
    for ev in partial_events:
        assert ev.data.get("run_id") == str(run.id)
        assert "deliverable_id" in ev.data


async def test_dedup_emit_does_not_publish_second_partial_event(tmp_path: Path) -> None:
    """A re-emit of the same ``external_ref`` is idempotent on the DB side —
    no second Deliverable row. It must ALSO be silent on the live wire: a dup
    LiveEvent would wake the PWA into refetching nothing.
    """
    bus = LiveEventBus()
    llm = ScriptedLlm(
        [
            LoopTurn(
                content="emit once",
                tool_calls=(
                    _tc(
                        "emit_deliverable",
                        artifact_type="pr",
                        summary="opened PR #9",
                        external_ref="github://acme/site/pull/9",
                    ),
                ),
            ),
            LoopTurn(
                content="emit same external_ref again",
                tool_calls=(
                    _tc(
                        "emit_deliverable",
                        artifact_type="pr",
                        summary="opened PR #9 (re-emit)",
                        external_ref="github://acme/site/pull/9",
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
        async with bus.subscribe(run.workspace_id) as queue:
            orch = RunOrchestrator(
                session=session,
                llm=llm,
                sandbox_manager=NoopSandboxManager(),
                live_event_bus=bus,
            )
            await orch.run(run=run, workspace_dir=tmp_path)
            events = await _drain(queue)

    partial_events = [e for e in events if e.event_type == EVENT_DELIVERABLE_PARTIAL]
    assert len(partial_events) == 1, f"expected dedupe → 1 partial event, got {len(partial_events)}"


# ---------------------------------------------------------------------------
# Delta 6 — no double-deliver: the verified terminal is NEVER also a partial
# ---------------------------------------------------------------------------


async def test_verified_terminal_is_not_marked_partial(tmp_path: Path) -> None:
    """When the loop emits a mid-loop partial AND lands the verified terminal,
    the terminal Deliverable's payload must NOT carry ``kind=mid_loop_partial``
    — the verified-final must remain distinguishable from partials. A double
    write (terminal payload tagged as partial) would silently turn every
    verified-final into a partial in any consumer that keys off ``kind``.
    """
    llm = ScriptedLlm(
        [
            LoopTurn(
                content="one partial then verify",
                tool_calls=(
                    _tc(
                        "emit_deliverable",
                        artifact_type="pr",
                        summary="opened PR",
                        external_ref="github://acme/site/pull/42",
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
        # 1 partial (PR) + 1 verified-final (CODE).
        assert len(deliverables) == 2

        partials = [
            d
            for d in deliverables
            if isinstance(d.payload, dict) and d.payload.get("kind") == PARTIAL_DELIVERABLE_KIND
        ]
        finals = [
            d
            for d in deliverables
            if not isinstance(d.payload, dict) or d.payload.get("kind") != PARTIAL_DELIVERABLE_KIND
        ]

        assert len(partials) == 1, "exactly one mid-loop partial expected"
        assert partials[0].deliverable_type == DeliverableType.PR
        assert len(finals) == 1, "exactly one verified-final expected"
        assert finals[0].deliverable_type == DeliverableType.CODE
        # The verified-final must NOT carry the partial kind tag.
        final_payload = finals[0].payload or {}
        assert isinstance(final_payload, dict)
        assert final_payload.get("kind") != PARTIAL_DELIVERABLE_KIND
