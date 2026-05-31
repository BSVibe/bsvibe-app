"""B12a — Safe Mode queue as a per-Run transactional container.

Workflow §1.2 + §3 + §11.2: each Run can emit MANY partial Deliver events;
when the workspace is in Safe Mode, every one of those events lands as a
queue item carrying the SAME ``run_id``. The founder approves them together
(per-Run), not one-by-one.

This test pins:

* ``SafeModeQueueItemRow.run_id`` is settable + persisted
* the DeliveryWorker threads each event's ``run_id`` onto the queue item
* a new ``POST /api/v1/safemode/runs/{run_id}/approve`` flips ALL pending
  items for that run together (N items, ONE call, all dispatched)
* per-deliverable approve is unchanged (back-compat)
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
from backend.execution.db import (
    Deliverable,
    DeliverableType,
    ExecutionRun,
    RunStatus,
)
from backend.workers.delivery_worker import DeliveryWorker, DeliveryWorkerConfig
from backend.workflow.application.safe_mode_queue import SafeModeQueue
from backend.workflow.domain.delivery import ActionResult, DeliveryResult
from backend.workflow.infrastructure.delivery.db import (
    DeliveryEventRow,
    SafeModeQueueItemRow,
    SafeModeStatus,
)
from backend.workspaces.db import WorkspaceRow

from .._support import db_engine, fake_current_user

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
    app.dependency_overrides[get_delivery_dispatcher] = lambda: sink

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _seed_run_with_n_artifacts(
    sf_: async_sessionmaker[AsyncSession],
    *,
    workspace_id: uuid.UUID,
    n: int,
    safe_mode: bool = True,
) -> tuple[uuid.UUID, list[uuid.UUID]]:
    """Seed one ExecutionRun + N Deliverables + N DeliveryEvents (each
    threading the run_id). Returns ``(run_id, [deliverable_ids…])``.

    FK-safe (PG enforces): TriggerEvent isn't required here because the run is
    seeded as a directly-stood-up row; the Deliverable -> ExecutionRun FK is the
    only one we cross."""
    run_id = uuid.uuid4()
    deliverables: list[uuid.UUID] = []
    async with sf_() as s:
        s.add(WorkspaceRow(id=workspace_id, name="acme", safe_mode=safe_mode))
        s.add(
            ExecutionRun(
                id=run_id,
                workspace_id=workspace_id,
                status=RunStatus.RUNNING,
                payload={"intent_text": "ship release"},
            )
        )
        await s.flush()
        for i in range(n):
            d_id = uuid.uuid4()
            deliverables.append(d_id)
            s.add(
                Deliverable(
                    id=d_id,
                    run_id=run_id,
                    workspace_id=workspace_id,
                    deliverable_type=DeliverableType.CODE,
                    payload={"summary": f"part {i + 1}"},
                )
            )
            s.add(
                DeliveryEventRow(
                    id=uuid.uuid4(),
                    workspace_id=workspace_id,
                    run_id=run_id,
                    deliverable_id=d_id,
                    artifact_type=DeliverableType.CODE.value,
                    payload={},
                    created_at=datetime.now(tz=UTC),
                )
            )
        await s.commit()
    return run_id, deliverables


# --------------------------------------------------------------------------
# Schema — SafeModeQueueItemRow carries run_id.
# --------------------------------------------------------------------------


async def test_queue_item_row_persists_run_id(sf: async_sessionmaker[AsyncSession]) -> None:
    ws = uuid.uuid4()
    run_id = uuid.uuid4()
    async with sf() as s:
        q = SafeModeQueue(s)
        item_id = await q.enqueue(workspace_id=ws, deliverable_id=uuid.uuid4(), run_id=run_id)
        await s.commit()
    async with sf() as s:
        row = await s.get(SafeModeQueueItemRow, item_id)
        assert row is not None
        assert row.run_id == run_id


# --------------------------------------------------------------------------
# DeliveryWorker threads run_id from the event onto the queue item.
# --------------------------------------------------------------------------


async def test_delivery_worker_threads_run_id_onto_queue_items(
    sf: async_sessionmaker[AsyncSession],
    workspace_id: uuid.UUID,
    sink: _SinkDispatcher,
) -> None:
    run_id, _ = await _seed_run_with_n_artifacts(sf, workspace_id=workspace_id, n=3, safe_mode=True)
    worker = DeliveryWorker(
        session_factory=sf,
        dispatcher=sink,
        config=DeliveryWorkerConfig(batch_size=10, poll_interval_s=0.01),
    )
    assert await worker.drain_once() == 3
    assert sink.dispatched == []

    async with sf() as s:
        items = (await s.execute(select(SafeModeQueueItemRow))).scalars().all()
        assert len(items) == 3
        # ALL three queue items share the run_id.
        assert {i.run_id for i in items} == {run_id}


# --------------------------------------------------------------------------
# Founder-facing: list queue grouped by run; approve-all-for-run.
# --------------------------------------------------------------------------


async def test_list_queue_groups_pending_items_by_run(
    client: httpx.AsyncClient,
    sf: async_sessionmaker[AsyncSession],
    workspace_id: uuid.UUID,
    sink: _SinkDispatcher,
) -> None:
    run_id, _ = await _seed_run_with_n_artifacts(sf, workspace_id=workspace_id, n=3, safe_mode=True)
    worker = DeliveryWorker(session_factory=sf, dispatcher=sink)
    await worker.drain_once()

    resp = await client.get("/api/v1/safemode/queue/by-run")
    assert resp.status_code == 200, resp.text
    groups = resp.json()
    assert isinstance(groups, list)
    assert len(groups) == 1
    grp = groups[0]
    assert grp["run_id"] == str(run_id)
    assert len(grp["items"]) == 3
    for item in grp["items"]:
        assert item["status"] == "pending"


async def test_approve_run_dispatches_all_pending_items_for_run(
    client: httpx.AsyncClient,
    sf: async_sessionmaker[AsyncSession],
    workspace_id: uuid.UUID,
    sink: _SinkDispatcher,
) -> None:
    run_id, deliverable_ids = await _seed_run_with_n_artifacts(
        sf, workspace_id=workspace_id, n=3, safe_mode=True
    )
    worker = DeliveryWorker(session_factory=sf, dispatcher=sink)
    await worker.drain_once()

    resp = await client.post(f"/api/v1/safemode/runs/{run_id}/approve")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["run_id"] == str(run_id)
    assert body["approved_count"] == 3
    assert body["dispatched_count"] == 3

    assert len(sink.dispatched) == 3
    dispatched_ids = {kw["deliverable_id"] for kw in sink.dispatched}
    assert dispatched_ids == set(deliverable_ids)

    async with sf() as s:
        rows = (await s.execute(select(SafeModeQueueItemRow))).scalars().all()
        assert {r.status for r in rows} == {SafeModeStatus.APPROVED}


async def test_approve_run_unknown_run_404(client: httpx.AsyncClient) -> None:
    resp = await client.post(f"/api/v1/safemode/runs/{uuid.uuid4()}/approve")
    assert resp.status_code == 404


# --------------------------------------------------------------------------
# Back-compat regression — per-deliverable approve still works.
# --------------------------------------------------------------------------


async def test_per_deliverable_approve_still_works_back_compat(
    client: httpx.AsyncClient,
    sf: async_sessionmaker[AsyncSession],
    workspace_id: uuid.UUID,
    sink: _SinkDispatcher,
) -> None:
    """Existing /api/v1/safemode/{item_id}/approve endpoint must keep working
    even after B12a — the new endpoint is additive."""
    run_id, _ = await _seed_run_with_n_artifacts(sf, workspace_id=workspace_id, n=2, safe_mode=True)
    worker = DeliveryWorker(session_factory=sf, dispatcher=sink)
    await worker.drain_once()

    listing = (await client.get("/api/v1/safemode/queue")).json()
    assert len(listing) == 2
    one_item_id = listing[0]["id"]

    resp = await client.post(f"/api/v1/safemode/{one_item_id}/approve")
    assert resp.status_code == 200
    assert resp.json()["status"] == "approved"
    assert resp.json()["dispatched"] is True
    assert len(sink.dispatched) == 1

    # The other queue item is still pending.
    remaining = (await client.get("/api/v1/safemode/queue")).json()
    assert len(remaining) == 1
    assert remaining[0]["id"] != one_item_id

    # An approve_run for the same run should now drain just the remaining one.
    resp = await client.post(f"/api/v1/safemode/runs/{run_id}/approve")
    assert resp.status_code == 200
    body = resp.json()
    assert body["approved_count"] == 1
    assert len(sink.dispatched) == 2
