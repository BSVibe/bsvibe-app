"""Safe Mode delivery gating end-to-end — the deliver-side half of §11.2.

Sibling of ``test_direct_path_e2e.py``. Where the Direct path proves a
founder-direct request flows straight out to the sink, this proves that when
the workspace is in **Safe Mode** the same verified Deliverable is *held* for
founder approval instead of dispatched, and only goes out once the founder
approves via ``/api/v1/safemode/{item_id}/approve``:

    (verified run) → Deliverable + DeliveryEventRow
      → DeliveryWorker.drain_once     → SafeModeQueueItem (pending), NO dispatch
      → POST /api/v1/safemode/{id}/approve → item APPROVED + dispatched to sink

Plus the deny branch: deny → DENIED, no dispatch.

Ticks the worker exactly once (``drain_once``), never the infinite poll loop.
In-memory SQLite by default, real Postgres when ``BSVIBE_DATABASE_URL`` is set
(mirrors the other glue tests).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.api.deps import (
    get_current_user,
    get_current_user_row,
    get_db_session,
    get_workspace_id,
)
from backend.api.main import create_app
from backend.api.v1.safemode import get_delivery_dispatcher
from backend.delivery.db import (
    DeliveryEventRow,
    SafeModeQueueItemRow,
    SafeModeStatus,
)
from backend.delivery.schema import ActionResult, DeliveryResult
from backend.execution.db import (
    Deliverable,
    DeliverableType,
    ExecutionRun,
    RunStatus,
)
from backend.workers.delivery_worker import DeliveryWorker, DeliveryWorkerConfig
from backend.workspaces.db import WorkspaceRow

from .._support import db_engine, fake_current_user

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def sf():
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


class _SinkDispatcher:
    """An in-test ``PluginDispatchAdapter`` — records what was dispatched."""

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


@pytest.fixture
def founder_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def workspace_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def sink() -> _SinkDispatcher:
    return _SinkDispatcher()


@pytest_asyncio.fixture
async def client(sf, founder_id: uuid.UUID, workspace_id: uuid.UUID, sink: _SinkDispatcher):
    app = create_app()

    def _ws() -> uuid.UUID:
        return workspace_id

    def _user_row() -> SimpleNamespace:
        return SimpleNamespace(id=founder_id)

    async def _session():
        async with sf() as s:
            yield s

    app.dependency_overrides[get_current_user] = fake_current_user()
    app.dependency_overrides[get_workspace_id] = _ws
    app.dependency_overrides[get_current_user_row] = _user_row
    app.dependency_overrides[get_db_session] = _session
    # The approve route dispatches through the same in-test sink the worker uses.
    app.dependency_overrides[get_delivery_dispatcher] = lambda: sink

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _seed_safe_mode_deliverable(
    sf_: async_sessionmaker[AsyncSession],
    *,
    workspace_id: uuid.UUID,
    safe_mode: bool,
) -> uuid.UUID:
    """Seed a Safe-Mode workspace + a verified Deliverable + its DeliveryEvent.

    Returns the deliverable id. Stands in for the upstream Direct chain (proven
    end-to-end in ``test_direct_path_e2e.py``) so this test stays focused on the
    delivery gate.
    """
    deliverable_id = uuid.uuid4()
    run_id = uuid.uuid4()
    async with sf_() as s:
        s.add(WorkspaceRow(id=workspace_id, name="acme", safe_mode=safe_mode))
        # ExecutionRun first — Deliverable.run_id FKs to it (PG enforces; SQLite
        # does not, but the FK exists, so seed it to mirror the real chain).
        s.add(
            ExecutionRun(
                id=run_id,
                workspace_id=workspace_id,
                status=RunStatus.REVIEW_READY,
                payload={},
            )
        )
        await s.flush()
        s.add(
            Deliverable(
                id=deliverable_id,
                run_id=run_id,
                workspace_id=workspace_id,
                deliverable_type=DeliverableType.CODE,
                payload={"artifact_refs": ["answer.txt"], "summary": "done"},
            )
        )
        s.add(
            DeliveryEventRow(
                id=uuid.uuid4(),
                workspace_id=workspace_id,
                deliverable_id=deliverable_id,
                artifact_type=DeliverableType.CODE.value,
                payload={},
                created_at=datetime.now(tz=UTC),
            )
        )
        await s.commit()
    return deliverable_id


# --------------------------------------------------------------------------
# Safe Mode ON — hold for approval, then approve dispatches.
# --------------------------------------------------------------------------


async def test_safe_mode_holds_then_approve_dispatches(
    client: httpx.AsyncClient,
    sf: async_sessionmaker[AsyncSession],
    workspace_id: uuid.UUID,
    sink: _SinkDispatcher,
) -> None:
    deliverable_id = await _seed_safe_mode_deliverable(
        sf, workspace_id=workspace_id, safe_mode=True
    )

    # 1. DeliveryWorker drains the event → enqueues a PENDING queue item, NOT dispatched.
    worker = DeliveryWorker(
        session_factory=sf,
        dispatcher=sink,
        config=DeliveryWorkerConfig(batch_size=10, poll_interval_s=0.01),
    )
    assert await worker.drain_once() == 1
    assert sink.dispatched == []  # Safe Mode held it back

    async with sf() as s:
        items = (await s.execute(select(SafeModeQueueItemRow))).scalars().all()
        assert len(items) == 1
        assert items[0].status is SafeModeStatus.PENDING
        assert items[0].deliverable_id == deliverable_id
        # The source event was consumed off the queue.
        assert (await s.execute(select(DeliveryEventRow))).first() is None

    # 2. GET the queue surfaces the pending item (with the compensation_tier field).
    resp = await client.get("/api/v1/safemode/queue")
    assert resp.status_code == 200, resp.text
    listed = resp.json()
    assert len(listed) == 1
    item_id = listed[0]["id"]
    assert listed[0]["status"] == "pending"
    assert listed[0]["deliverable_id"] == str(deliverable_id)
    assert "compensation_tier" in listed[0]

    # 3. POST approve → item APPROVED + deliverable dispatched to the sink.
    approve = await client.post(f"/api/v1/safemode/{item_id}/approve")
    assert approve.status_code == 200, approve.text
    assert approve.json() == {"item_id": item_id, "status": "approved", "dispatched": True}

    assert len(sink.dispatched) == 1
    assert sink.dispatched[0]["deliverable_id"] == deliverable_id
    assert sink.dispatched[0]["workspace_id"] == workspace_id
    assert sink.dispatched[0]["artifact_type"] == "code"

    async with sf() as s:
        item = await s.get(SafeModeQueueItemRow, uuid.UUID(item_id))
        assert item is not None
        assert item.status is SafeModeStatus.APPROVED
        assert item.decided_at is not None

    # 4. The queue no longer lists it (no longer pending).
    resp = await client.get("/api/v1/safemode/queue")
    assert resp.json() == []


async def test_safe_mode_deny_does_not_dispatch(
    client: httpx.AsyncClient,
    sf: async_sessionmaker[AsyncSession],
    workspace_id: uuid.UUID,
    sink: _SinkDispatcher,
) -> None:
    await _seed_safe_mode_deliverable(sf, workspace_id=workspace_id, safe_mode=True)

    worker = DeliveryWorker(session_factory=sf, dispatcher=sink)
    assert await worker.drain_once() == 1
    assert sink.dispatched == []

    resp = await client.get("/api/v1/safemode/queue")
    item_id = resp.json()[0]["id"]

    deny = await client.post(f"/api/v1/safemode/{item_id}/deny", json={"reason": "off-brand"})
    assert deny.status_code == 200, deny.text
    assert deny.json() == {"item_id": item_id, "status": "denied", "dispatched": False}

    # Denied → no dispatch ever happened.
    assert sink.dispatched == []
    async with sf() as s:
        item = await s.get(SafeModeQueueItemRow, uuid.UUID(item_id))
        assert item is not None
        assert item.status is SafeModeStatus.DENIED


async def test_approve_unknown_item_404(
    client: httpx.AsyncClient,
) -> None:
    resp = await client.post(f"/api/v1/safemode/{uuid.uuid4()}/approve")
    assert resp.status_code == 404


async def test_deny_unknown_item_404(
    client: httpx.AsyncClient,
) -> None:
    resp = await client.post(f"/api/v1/safemode/{uuid.uuid4()}/deny", json={"reason": "x"})
    assert resp.status_code == 404


# --------------------------------------------------------------------------
# Safe Mode OFF — the worker dispatches straight out (Direct-path behavior).
# --------------------------------------------------------------------------


async def test_safe_mode_off_dispatches_straight_out(
    sf: async_sessionmaker[AsyncSession],
    workspace_id: uuid.UUID,
    sink: _SinkDispatcher,
) -> None:
    deliverable_id = await _seed_safe_mode_deliverable(
        sf, workspace_id=workspace_id, safe_mode=False
    )

    worker = DeliveryWorker(session_factory=sf, dispatcher=sink)
    assert await worker.drain_once() == 1

    # Dispatched out, nothing enqueued.
    assert len(sink.dispatched) == 1
    assert sink.dispatched[0]["deliverable_id"] == deliverable_id
    async with sf() as s:
        assert (await s.execute(select(SafeModeQueueItemRow))).first() is None


async def test_no_workspace_row_defaults_to_direct_dispatch(
    sf: async_sessionmaker[AsyncSession],
    sink: _SinkDispatcher,
) -> None:
    """An unseeded workspace (no WorkspaceRow) defaults to direct dispatch —
    this is what keeps ``test_direct_path_e2e.py`` green."""
    ws = uuid.uuid4()
    async with sf() as s:
        s.add(
            DeliveryEventRow(
                id=uuid.uuid4(),
                workspace_id=ws,
                deliverable_id=uuid.uuid4(),
                artifact_type="code",
                payload={},
                created_at=datetime.now(tz=UTC),
            )
        )
        await s.commit()

    worker = DeliveryWorker(session_factory=sf, dispatcher=sink)
    assert await worker.drain_once() == 1
    assert len(sink.dispatched) == 1
    async with sf() as s:
        assert (await s.execute(select(SafeModeQueueItemRow))).first() is None
