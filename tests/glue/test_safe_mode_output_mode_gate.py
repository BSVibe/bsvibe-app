"""D3 — Safe Mode gates per-Run by the triggering Resource's ``output_mode``.

Synthesis §11 / Workflow §10.5: the Safe Mode decision is keyed to the
triggering Resource's ``output_mode`` (the per-Resource 3-knob value) and
applied **per-Run**, NOT a single global workspace switch. ``workspace.safe_mode``
remains a GLOBAL OVERRIDE — when on, everything queues regardless of output_mode.

Asserted deltas (delta, not post-state):

1. **Per-Run divergence**: a Run whose triggering Resource ``output_mode ==
   "safe"`` is ENQUEUED even when ``workspace.safe_mode == False``; a Run with
   ``output_mode == "direct"`` DELIVERS DIRECTLY (same workspace flag off).
   (Today only the workspace flag matters — a no-op gate cannot pass both.)
2. **No-regression**: a Run with NO resolved ``output_mode`` behaves exactly as
   today under ``workspace.safe_mode`` (on → queues, off → delivers).
3. **Global override**: ``workspace.safe_mode == True`` still queues an
   ``output_mode == "direct"`` Run.
4. **Lifecycle**: an approved item transitions to delivered+archived; an
   expired pending item is swept. State TRANSITIONS asserted, not just final.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.connectors.db import ConnectorAccountRow
from backend.identity.workspaces_db import (
    ProductRow,
    ResourceBindingRow,
    WorkspaceRow,
)
from backend.workflow.application.safe_mode_queue import SafeModeQueue
from backend.workflow.domain.delivery import ActionResult, DeliveryResult
from backend.workflow.infrastructure.db import (
    Deliverable,
    DeliverableType,
    ExecutionRun,
    RunStatus,
)
from backend.workflow.infrastructure.delivery.db import (
    DeliveryEventRow,
    SafeModeQueueItemRow,
    SafeModeStatus,
)
from backend.workflow.infrastructure.workers.delivery_worker import (
    DeliveryWorker,
    DeliveryWorkerConfig,
    resolve_output_mode_gate,
)

from .._support import db_engine

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def sf():
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


class _SinkDispatcher:
    def __init__(self) -> None:
        self.dispatched: list[dict[str, Any]] = []

    async def dispatch(self, **kwargs: Any) -> DeliveryResult:
        self.dispatched.append(kwargs)
        return DeliveryResult(
            workspace_id=kwargs["workspace_id"],
            deliverable_id=kwargs["deliverable_id"],
            artifact_type=kwargs["artifact_type"],
            actions=[ActionResult(action="sink", succeeded=True)],
            delivered_at=datetime.now(tz=UTC),
        )


async def _seed_run(
    sf_: async_sessionmaker[AsyncSession],
    *,
    workspace_id: uuid.UUID,
    safe_mode: bool,
    output_mode: str | None,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Seed a workspace + (optional) binding + run + deliverable + delivery event.

    When ``output_mode`` is given, a real ResourceBinding is created and its id
    is stashed on the run payload (``binding_id``) exactly as Receive→open_run
    would — so the gate resolves the triggering Resource's output_mode from the
    binding, not a synthetic field. ``output_mode=None`` seeds a run with NO
    binding (the no-regression path that falls back to the workspace flag).
    Returns ``(run_id, deliverable_id)``.
    """
    run_id = uuid.uuid4()
    deliverable_id = uuid.uuid4()
    run_payload: dict[str, Any] = {"intent_text": "ship release"}
    async with sf_() as s:
        s.add(WorkspaceRow(id=workspace_id, name="acme", safe_mode=safe_mode))
        await s.flush()
        if output_mode is not None:
            product_id = uuid.uuid4()
            account_id = uuid.uuid4()
            binding_id = uuid.uuid4()
            s.add(ProductRow(id=product_id, workspace_id=workspace_id, name="P", slug="p"))
            s.add(
                ConnectorAccountRow(
                    id=account_id,
                    workspace_id=workspace_id,
                    connector="github",
                    webhook_token=f"tok-{uuid.uuid4().hex}",
                    signing_secret_ciphertext="cipher",
                )
            )
            await s.flush()
            s.add(
                ResourceBindingRow(
                    id=binding_id,
                    workspace_id=workspace_id,
                    product_id=product_id,
                    connector_account_id=account_id,
                    resource_id="owner/repo",
                    output_mode=output_mode,
                )
            )
            run_payload["binding_id"] = str(binding_id)
        s.add(
            ExecutionRun(
                id=run_id,
                workspace_id=workspace_id,
                status=RunStatus.RUNNING,
                payload=run_payload,
            )
        )
        await s.flush()
        s.add(
            Deliverable(
                id=deliverable_id,
                run_id=run_id,
                workspace_id=workspace_id,
                deliverable_type=DeliverableType.CODE,
                payload={"summary": "part"},
            )
        )
        s.add(
            DeliveryEventRow(
                id=uuid.uuid4(),
                workspace_id=workspace_id,
                run_id=run_id,
                deliverable_id=deliverable_id,
                artifact_type=DeliverableType.CODE.value,
                payload={},
                created_at=datetime.now(tz=UTC),
            )
        )
        await s.commit()
    return run_id, deliverable_id


def _worker(sf_, sink: _SinkDispatcher) -> DeliveryWorker:
    return DeliveryWorker(
        session_factory=sf_,
        dispatcher=sink,
        config=DeliveryWorkerConfig(batch_size=10, poll_interval_s=0.01),
    )


# ---------------------------------------------------------------------------
# Pure gate decision — precedence: workspace override > output_mode > default.
# ---------------------------------------------------------------------------


async def test_gate_decision_precedence() -> None:
    # Global override wins regardless of output_mode.
    assert resolve_output_mode_gate(workspace_safe_mode=True, output_mode="direct") is True
    assert resolve_output_mode_gate(workspace_safe_mode=True, output_mode="safe") is True
    assert resolve_output_mode_gate(workspace_safe_mode=True, output_mode=None) is True
    # Override off → output_mode decides.
    assert resolve_output_mode_gate(workspace_safe_mode=False, output_mode="safe") is True
    assert resolve_output_mode_gate(workspace_safe_mode=False, output_mode="direct") is False
    # Override off + no resolved output_mode → today's behavior (deliver).
    assert resolve_output_mode_gate(workspace_safe_mode=False, output_mode=None) is False


# ---------------------------------------------------------------------------
# Delta 1 — per-Run divergence with the workspace flag OFF.
# ---------------------------------------------------------------------------


async def test_per_run_safe_output_mode_queues_even_when_workspace_flag_off(
    sf: async_sessionmaker[AsyncSession],
) -> None:
    ws = uuid.uuid4()
    run_id, _ = await _seed_run(sf, workspace_id=ws, safe_mode=False, output_mode="safe")
    sink = _SinkDispatcher()
    assert await _worker(sf, sink).drain_once() == 1
    # Queued, NOT dispatched — even though the workspace flag is OFF.
    assert sink.dispatched == []
    async with sf() as s:
        items = (await s.execute(select(SafeModeQueueItemRow))).scalars().all()
        assert len(items) == 1
        assert items[0].run_id == run_id
        assert items[0].status is SafeModeStatus.PENDING


async def test_per_run_direct_output_mode_delivers_when_workspace_flag_off(
    sf: async_sessionmaker[AsyncSession],
) -> None:
    ws = uuid.uuid4()
    _, deliverable_id = await _seed_run(sf, workspace_id=ws, safe_mode=False, output_mode="direct")
    sink = _SinkDispatcher()
    assert await _worker(sf, sink).drain_once() == 1
    # Delivered directly — no queue item.
    assert len(sink.dispatched) == 1
    assert sink.dispatched[0]["deliverable_id"] == deliverable_id
    async with sf() as s:
        items = (await s.execute(select(SafeModeQueueItemRow))).scalars().all()
        assert items == []


# ---------------------------------------------------------------------------
# Delta 2 — no-regression: no output_mode → today's workspace-flag behavior.
# ---------------------------------------------------------------------------


async def test_no_output_mode_workspace_flag_on_queues(
    sf: async_sessionmaker[AsyncSession],
) -> None:
    ws = uuid.uuid4()
    await _seed_run(sf, workspace_id=ws, safe_mode=True, output_mode=None)
    sink = _SinkDispatcher()
    assert await _worker(sf, sink).drain_once() == 1
    assert sink.dispatched == []
    async with sf() as s:
        items = (await s.execute(select(SafeModeQueueItemRow))).scalars().all()
        assert len(items) == 1


async def test_no_output_mode_workspace_flag_off_delivers(
    sf: async_sessionmaker[AsyncSession],
) -> None:
    ws = uuid.uuid4()
    await _seed_run(sf, workspace_id=ws, safe_mode=False, output_mode=None)
    sink = _SinkDispatcher()
    assert await _worker(sf, sink).drain_once() == 1
    assert len(sink.dispatched) == 1
    async with sf() as s:
        items = (await s.execute(select(SafeModeQueueItemRow))).scalars().all()
        assert items == []


# ---------------------------------------------------------------------------
# Delta 3 — global override: workspace flag ON queues a direct-output Run.
# ---------------------------------------------------------------------------


async def test_workspace_flag_on_overrides_direct_output_mode(
    sf: async_sessionmaker[AsyncSession],
) -> None:
    ws = uuid.uuid4()
    await _seed_run(sf, workspace_id=ws, safe_mode=True, output_mode="direct")
    sink = _SinkDispatcher()
    assert await _worker(sf, sink).drain_once() == 1
    # Workspace override forces the queue even though output_mode == direct.
    assert sink.dispatched == []
    async with sf() as s:
        items = (await s.execute(select(SafeModeQueueItemRow))).scalars().all()
        assert len(items) == 1


# ---------------------------------------------------------------------------
# Delta 4 — queue lifecycle: approved→delivered→archived; expired sweep.
# ---------------------------------------------------------------------------


async def test_mark_delivered_then_archive_transitions(
    sf: async_sessionmaker[AsyncSession],
) -> None:
    ws = uuid.uuid4()
    async with sf() as s:
        q = SafeModeQueue(s)
        item_id = await q.enqueue(workspace_id=ws, deliverable_id=uuid.uuid4())
        # pending → approved
        assert await q.approve(workspace_id=ws, item_id=item_id, actor_id=uuid.uuid4()) is True
        await s.commit()
        row = await s.get(SafeModeQueueItemRow, item_id)
        assert row.status is SafeModeStatus.APPROVED

        # approved → delivered
        assert await q.mark_delivered(workspace_id=ws, item_id=item_id) is True
        await s.commit()
        row = await s.get(SafeModeQueueItemRow, item_id)
        assert row.status is SafeModeStatus.DELIVERED

        # delivered → archived
        assert await q.archive(workspace_id=ws, item_id=item_id) is True
        await s.commit()
        row = await s.get(SafeModeQueueItemRow, item_id)
        assert row.status is SafeModeStatus.ARCHIVED

        # archived → deleted
        assert await q.mark_deleted(workspace_id=ws, item_id=item_id) is True
        await s.commit()
        row = await s.get(SafeModeQueueItemRow, item_id)
        assert row.status is SafeModeStatus.DELETED


async def test_mark_delivered_requires_approved(
    sf: async_sessionmaker[AsyncSession],
) -> None:
    """A pending (un-approved) item cannot be marked delivered — the
    lifecycle edge approved→delivered is enforced, not a free set."""
    ws = uuid.uuid4()
    async with sf() as s:
        q = SafeModeQueue(s)
        item_id = await q.enqueue(workspace_id=ws, deliverable_id=uuid.uuid4())
        await s.commit()
        assert await q.mark_delivered(workspace_id=ws, item_id=item_id) is False
        row = await s.get(SafeModeQueueItemRow, item_id)
        assert row.status is SafeModeStatus.PENDING


async def test_expire_sweep_moves_stale_pending(
    sf: async_sessionmaker[AsyncSession],
) -> None:
    ws = uuid.uuid4()
    async with sf() as s:
        # Hand-craft an overdue pending row.
        row = SafeModeQueueItemRow(
            id=uuid.uuid4(),
            workspace_id=ws,
            deliverable_id=uuid.uuid4(),
            status=SafeModeStatus.PENDING,
            expires_at=datetime.now(tz=UTC) - timedelta(days=1),
            extension_count=0,
            created_at=datetime.now(tz=UTC) - timedelta(days=91),
        )
        s.add(row)
        await s.commit()
        item_id = row.id

        q = SafeModeQueue(s)
        swept = await q.expire(workspace_id=ws)
        await s.commit()
        assert swept == 1

    # Read in a FRESH session: ``expire`` runs a core bulk UPDATE, so the row
    # in the writing session's identity map is stale until reloaded.
    async with sf() as s2:
        fresh = await s2.get(SafeModeQueueItemRow, item_id)
        # pending → expired (transition), and decided_at stamped.
        assert fresh.status is SafeModeStatus.EXPIRED
        assert fresh.decided_at is not None
