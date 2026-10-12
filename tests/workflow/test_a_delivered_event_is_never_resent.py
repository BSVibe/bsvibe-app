"""A delivery event that already reached the founder is never sent again.

Measured 2026-10-12: the weekly BStockReport schedule's run published its report
through ``emit_deliverable(artifact_type="report")``. The delivery worker sent it
to Telegram, then building the ``DeliveryResult`` raised — ``"report"`` is not an
``ArtifactType`` literal. The raise left the ``delivery_events`` row in place, so
every ~6 s tick sent the same report again (30 sends in the first minute), and
the run stayed ``running`` because the auto-resolve after delivery never ran.

Three guards:
1. A free-form artifact_type the work LLM chose reaches the result as the
   deliverable-level type it was parked under (``direct_output``) — the adapter
   no longer raises AFTER the outbound went out.
2. Book-keeping that runs after a successful send (compensation handles, run
   auto-resolve) cannot turn into a resend if it raises.
3. A row that keeps failing is dropped after a bounded number of attempts
   instead of re-claimed every tick forever.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.config import get_settings
from backend.data.rls import workspace_session_scope
from backend.identity.workspaces_db import WorkspaceRow
from backend.workflow.application.delivery.connector_dispatch import ConnectorDeliveryAdapter
from backend.workflow.domain.delivery import ActionResult, DeliveryResult
from backend.workflow.infrastructure.db import Deliverable, ExecutionRun, RunStatus
from backend.workflow.infrastructure.delivery.db import DeliveryEventRow
from backend.workflow.infrastructure.workers import delivery_worker as dw
from backend.workflow.infrastructure.workers.delivery_worker import (
    DeliveryWorker,
    DeliveryWorkerConfig,
    dispatch_delivery,
)

from .._support import db_engine

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def db() -> AsyncIterator[Any]:
    get_settings.cache_clear()
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)
    get_settings.cache_clear()


class _FakeCipher:
    def decrypt(self, token: str) -> str:
        return "tok"


async def _seed_event(db_, *, artifact_type: str) -> tuple[uuid.UUID, uuid.UUID]:
    """A direct (no Safe Mode) run with one partial deliverable + its event."""
    ws, run_id, deliverable_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with db_() as s, workspace_session_scope(s, ws):
        s.add(WorkspaceRow(id=ws, name="ws-resend", safe_mode=False))
        await s.flush()
        s.add(ExecutionRun(id=run_id, workspace_id=ws, status=RunStatus.RUNNING, payload={}))
        await s.flush()
        s.add(
            Deliverable(
                id=deliverable_id,
                run_id=run_id,
                workspace_id=ws,
                deliverable_type="direct_output",
                payload={"kind": "partial", "artifact_type": artifact_type, "summary": "주간"},
            )
        )
        s.add(
            DeliveryEventRow(
                id=uuid.uuid4(),
                workspace_id=ws,
                run_id=run_id,
                deliverable_id=deliverable_id,
                artifact_type=artifact_type,
                payload={"summary": "주간"},
            )
        )
        await s.commit()
    return ws, deliverable_id


async def _pending(db_, ws: uuid.UUID) -> int:
    async with db_() as s, workspace_session_scope(s, ws):
        return len((await s.execute(select(DeliveryEventRow))).scalars().all())


# ---------------------------------------------------------------------------
# 1. The real adapter accepts the artifact_type the LLM chose
# ---------------------------------------------------------------------------


async def test_the_adapter_reports_a_free_form_type_as_direct_output(db) -> None:
    adapter = ConnectorDeliveryAdapter(session_factory=db, plugins_by_name={}, cipher=_FakeCipher())

    result = await adapter.dispatch(
        workspace_id=uuid.uuid4(), deliverable_id=uuid.uuid4(), artifact_type="report"
    )

    assert result.artifact_type == "direct_output"


async def test_a_known_type_is_kept(db) -> None:
    """Control — a real ArtifactType passes through untouched."""
    adapter = ConnectorDeliveryAdapter(session_factory=db, plugins_by_name={}, cipher=_FakeCipher())

    result = await adapter.dispatch(
        workspace_id=uuid.uuid4(), deliverable_id=uuid.uuid4(), artifact_type="code"
    )

    assert result.artifact_type == "code"


async def test_the_worker_drains_a_report_event_through_the_real_adapter(db) -> None:
    ws, _ = await _seed_event(db, artifact_type="report")
    adapter = ConnectorDeliveryAdapter(session_factory=db, plugins_by_name={}, cipher=_FakeCipher())

    await DeliveryWorker(session_factory=db, dispatcher=adapter).drain_once()

    assert await _pending(db, ws) == 0


# ---------------------------------------------------------------------------
# 2. Book-keeping after a successful send never becomes a resend
# ---------------------------------------------------------------------------


class _SendingDispatcher:
    def __init__(self) -> None:
        self.sends = 0

    async def dispatch(self, *, workspace_id, deliverable_id, artifact_type, **_: Any):
        self.sends += 1
        return DeliveryResult(
            workspace_id=workspace_id,
            deliverable_id=deliverable_id,
            artifact_type="direct_output",
            actions=[ActionResult(action="telegram.send", succeeded=True)],
        )


async def test_a_post_send_failure_does_not_raise(db, monkeypatch) -> None:
    async def _boom(*_a: Any, **_k: Any) -> int:
        raise RuntimeError("bookkeeping broke")

    monkeypatch.setattr(dw, "persist_compensation_handles", _boom)
    dispatcher = _SendingDispatcher()

    result = await dispatch_delivery(
        dispatcher,
        workspace_id=uuid.uuid4(),
        deliverable_id=uuid.uuid4(),
        artifact_type="direct_output",
        session_factory=db,
    )

    assert dispatcher.sends == 1
    assert result.actions[0].succeeded is True


async def test_a_post_send_failure_leaves_nothing_to_resend(db, monkeypatch) -> None:
    async def _boom(*_a: Any, **_k: Any) -> int:
        raise RuntimeError("bookkeeping broke")

    monkeypatch.setattr(dw, "persist_compensation_handles", _boom)
    ws, _ = await _seed_event(db, artifact_type="direct_output")
    dispatcher = _SendingDispatcher()
    worker = DeliveryWorker(session_factory=db, dispatcher=dispatcher)

    await worker.drain_once()
    await worker.drain_once()

    assert dispatcher.sends == 1
    assert await _pending(db, ws) == 0


# ---------------------------------------------------------------------------
# 3. A row that keeps failing is dropped after a bounded number of attempts
# ---------------------------------------------------------------------------


class _FailingDispatcher:
    def __init__(self) -> None:
        self.calls = 0

    async def dispatch(self, **_: Any):
        self.calls += 1
        raise RuntimeError("outbound down")


async def test_a_failing_row_is_retried_then_dropped(db) -> None:
    ws, _ = await _seed_event(db, artifact_type="direct_output")
    dispatcher = _FailingDispatcher()
    worker = DeliveryWorker(
        session_factory=db,
        dispatcher=dispatcher,
        config=DeliveryWorkerConfig(max_attempts=3),
    )

    for _ in range(3):
        await worker.drain_once()
    assert await _pending(db, ws) == 0
    await worker.drain_once()

    assert dispatcher.calls == 3


async def test_a_failing_row_survives_below_the_cap(db) -> None:
    """Control — at-least-once still holds for transient failures."""
    ws, _ = await _seed_event(db, artifact_type="direct_output")
    worker = DeliveryWorker(
        session_factory=db,
        dispatcher=_FailingDispatcher(),
        config=DeliveryWorkerConfig(max_attempts=3),
    )

    for _ in range(2):
        await worker.drain_once()

    assert await _pending(db, ws) == 1
