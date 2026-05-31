"""Tests for the audit→LiveEventBus bridge in ``safe_emit`` (B16).

``safe_emit`` is the canonical producer entry point every audit producer
calls (orchestrator, checkpoints, chat, delivery). B16 adds an in-process
fan-out hook for the high-signal subset (decision pending, run terminal,
delivery queued) so the PWA SSE channel wakes up the same moment the
durable outbox row lands.

Properties under test:
* Mapped audit types (``execution.decision.pending``,
  ``execution.loop.terminal``, ``delivery.queued``) fan out to the bus.
* Unmapped audit types (``execution.llm.turn``, ``execution.tool.call``,
  etc.) DO NOT fan out — they remain in the durable outbox only, so the
  SSE channel stays low-noise.
* Workspace isolation is preserved (the bridge keys by the event's
  ``workspace_id``).
* A bridge failure NEVER propagates back into the caller.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from backend.api.v1.live_events import (
    LiveEvent,
    LiveEventBus,
    get_live_event_bus,
    reset_live_event_bus_for_testing,
)
from backend.workflow.application.audit_events import (
    DecisionPending,
    DecisionResolved,
    LlmTurn,
    LoopTerminal,
    ToolCall,
)
from plugin.audit.emitter import AuditEmitter
from plugin.audit.events import AuditActor, AuditResource
from plugin.audit.service import safe_emit

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _fresh_bus():
    """Each test starts with a fresh singleton bus so listener counts are
    deterministic across cases."""
    reset_live_event_bus_for_testing()
    yield
    reset_live_event_bus_for_testing()


class _NullEmitter(AuditEmitter):
    """An emitter that succeeds without writing to a DB session — lets
    bridge tests skip the whole SQLAlchemy setup."""

    async def emit(self, event, *, session) -> None:  # type: ignore[override]
        return


async def _collect_one(bus: LiveEventBus, workspace_id: uuid.UUID) -> LiveEvent:
    """Subscribe and return the first event delivered for ``workspace_id``.

    Uses ``asyncio.timeout`` (3.11+) rather than a ``timeout=`` kwarg so the
    ruff ASYNC109 rule (test helpers shouldn't smuggle their own timeout
    parameter past pytest-asyncio) stays clean.
    """
    async with bus.subscribe(workspace_id) as queue:
        # Yield control so the subscriber is registered before the producer
        # publishes.
        await asyncio.sleep(0)
        async with asyncio.timeout(1.0):
            return await queue.get()


async def test_decision_pending_audit_bridges_to_sse_event() -> None:
    """An ``execution.decision.pending`` audit event becomes a
    ``decision.pending`` SSE event on the bus, keyed by workspace_id."""
    workspace_id = uuid.uuid4()
    run_id = uuid.uuid4()
    bus = get_live_event_bus()
    event = DecisionPending(
        actor=AuditActor(type="system", id="orch"),
        workspace_id=str(workspace_id),
        resource=AuditResource(type="execution_run", id=str(run_id)),
        data={"run_id": str(run_id), "decision_id": "d1"},
    )

    async def consume() -> LiveEvent:
        return await _collect_one(bus, workspace_id)

    consumer = asyncio.create_task(consume())
    await asyncio.sleep(0.05)
    await safe_emit(event, session=None, emitter=_NullEmitter())  # type: ignore[arg-type]
    received = await consumer

    assert received.event_type == "decision.pending"
    assert received.data["run_id"] == str(run_id)
    assert received.data["decision_id"] == "d1"
    assert received.data["resource_type"] == "execution_run"


async def test_loop_terminal_audit_bridges_to_run_terminal_sse_event() -> None:
    """``execution.loop.terminal`` → ``run.terminal`` SSE event."""
    workspace_id = uuid.uuid4()
    run_id = uuid.uuid4()
    bus = get_live_event_bus()
    event = LoopTerminal(
        actor=AuditActor(type="system", id="orch"),
        workspace_id=str(workspace_id),
        resource=AuditResource(type="execution_run", id=str(run_id)),
        data={"run_id": str(run_id), "outcome": "verified"},
    )

    consumer = asyncio.create_task(_collect_one(bus, workspace_id))
    await asyncio.sleep(0.05)
    await safe_emit(event, session=None, emitter=_NullEmitter())  # type: ignore[arg-type]
    received = await consumer

    assert received.event_type == "run.terminal"
    assert received.data["run_id"] == str(run_id)


async def test_llm_turn_audit_does_not_bridge_to_sse() -> None:
    """Audit events outside the high-signal subset (``execution.llm.turn``,
    ``execution.tool.call``, ``execution.decision.resolved``) MUST NOT show
    up on the bus — the SSE channel is intentionally narrow."""
    workspace_id = uuid.uuid4()
    bus = get_live_event_bus()

    # Subscribe FIRST so we can prove no event arrives.
    async def listen() -> LiveEvent | None:
        async with bus.subscribe(workspace_id) as queue:
            try:
                return await asyncio.wait_for(queue.get(), timeout=0.2)
            except TimeoutError:
                return None

    listener = asyncio.create_task(listen())
    await asyncio.sleep(0.05)

    for event_cls in (LlmTurn, ToolCall, DecisionResolved):
        event = event_cls(
            actor=AuditActor(type="system", id="orch"),
            workspace_id=str(workspace_id),
            data={},
        )
        await safe_emit(event, session=None, emitter=_NullEmitter())  # type: ignore[arg-type]

    received = await listener
    assert received is None  # nothing leaked onto the SSE channel


async def test_bridge_respects_workspace_isolation() -> None:
    """An audit emit for workspace A must NOT publish onto a workspace B
    subscriber — the bridge keys by the event's ``workspace_id``."""
    workspace_a = uuid.uuid4()
    workspace_b = uuid.uuid4()
    bus = get_live_event_bus()

    async def listen_b() -> LiveEvent | None:
        async with bus.subscribe(workspace_b) as queue:
            try:
                return await asyncio.wait_for(queue.get(), timeout=0.2)
            except TimeoutError:
                return None

    listener = asyncio.create_task(listen_b())
    await asyncio.sleep(0.05)
    event = DecisionPending(
        actor=AuditActor(type="system", id="orch"),
        workspace_id=str(workspace_a),
        data={"decision_id": "from-a"},
    )
    await safe_emit(event, session=None, emitter=_NullEmitter())  # type: ignore[arg-type]
    received = await listener
    assert received is None


async def test_bridge_failure_does_not_propagate() -> None:
    """A failure inside the LiveEventBus bridge must NEVER raise into the
    caller — the durable outbox row already landed, the SSE-wake is
    best-effort."""
    workspace_id = uuid.uuid4()
    event = DecisionPending(
        actor=AuditActor(type="system", id="orch"),
        workspace_id=str(workspace_id),
        data={},
    )

    # Monkey-patch the bus accessor to raise so the bridge's except clause
    # fires. The caller must still return normally.
    import plugin.audit.service as service_mod

    original_safe_emit = service_mod.safe_emit
    # The bridge soft-imports inside the function — patching that import
    # target makes it raise.
    import backend.api.v1.live_events as live_events_mod

    original_bus_getter = live_events_mod.get_live_event_bus

    def explode() -> object:
        raise RuntimeError("boom — live bus is on fire")

    live_events_mod.get_live_event_bus = explode  # type: ignore[assignment]
    try:
        # Must NOT raise.
        await original_safe_emit(event, session=None, emitter=_NullEmitter())  # type: ignore[arg-type]
    finally:
        live_events_mod.get_live_event_bus = original_bus_getter
