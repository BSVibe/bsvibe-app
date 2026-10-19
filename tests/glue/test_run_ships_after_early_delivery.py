"""A run whose output was delivered BEFORE it reached REVIEW_READY still ships.

Measured 2026-10-19, run ``4f5e9bfb`` (the scheduled BStockReport weekly report, output_mode
direct): the agent's ``emit_deliverable`` went out to Telegram at 00:31 while the run was still
RUNNING. Delivery success is where a run with no local ship path gets shipped
(``auto_resolve_run_on_delivery``), and that only acts on a REVIEW_READY run — so it did
nothing. The run reached REVIEW_READY four minutes later, and no further delivery ever
happened to complete it. It sat there, delivered, looking like it needed a review.

The root: "this run's output was delivered" was an EVENT handled at one moment, never a FACT
anyone could ask about later. So:

* a successful dispatch stamps ``Deliverable.delivered_at``;
* ``ship_if_delivered`` asks the facts — REVIEW_READY, no local ship path, something
  delivered (and not retracted), nothing still waiting to go out — and ships;
* it is asked at the REVIEW_READY transition AND swept by the delivery worker, so the order
  in which the two happen, or a race between their sessions, cannot strand a run.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.data.rls import workspace_session_scope
from backend.data.scoping import workspace_scope
from backend.workflow.application.agent_runner import AgentRunner
from backend.workflow.domain.delivery import ActionResult, DeliveryResult
from backend.workflow.infrastructure.db import (
    Deliverable,
    DeliverableType,
    ExecutionBase,
    ExecutionRun,
    RunStatus,
)
from backend.workflow.infrastructure.delivery.db import (
    DeliveryBase,
    DeliveryEventRow,
    SafeModeQueueItemRow,
    SafeModeStatus,
)
from backend.workflow.infrastructure.workers.delivery_worker import dispatch_delivery

from .._support import db_engine

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def sf():
    async with db_engine(ExecutionBase, DeliveryBase) as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


class _Dispatcher:
    def __init__(self, *, ok: bool) -> None:
        self.ok = ok

    async def dispatch(self, *, workspace_id: uuid.UUID, deliverable_id: uuid.UUID, **_: Any):
        return DeliveryResult(
            workspace_id=workspace_id,
            deliverable_id=deliverable_id,
            artifact_type="direct_output",
            actions=[
                ActionResult(
                    action="telegram:outbound:message",
                    succeeded=self.ok,
                    error=None if self.ok else "boom",
                )
            ],
            delivered_at=datetime.now(tz=UTC),
            error=None if self.ok else "boom",
        )


async def _seed(
    sf_: async_sessionmaker,
    *,
    status: RunStatus = RunStatus.RUNNING,
    payload: dict[str, Any] | None = None,
    delivered: bool = False,
    retracted: bool = False,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    ws, run_id, d_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    now = datetime.now(tz=UTC)
    async with sf_() as s, workspace_session_scope(s, ws):
        s.add(
            ExecutionRun(
                id=run_id,
                workspace_id=ws,
                product_id=uuid.uuid4(),
                status=status,
                payload=payload or {},
                created_at=now,
                updated_at=now,
            )
        )
        await s.flush()
        s.add(
            Deliverable(
                id=d_id,
                run_id=run_id,
                workspace_id=ws,
                deliverable_type=DeliverableType.DIRECT_OUTPUT,
                payload={"kind": "mid_loop_partial", "summary": "numbers"},
                delivered_at=now if delivered else None,
                retracted_at=now if retracted else None,
                created_at=now,
            )
        )
        await s.commit()
    return ws, run_id, d_id


async def _status(sf_: async_sessionmaker, ws: uuid.UUID, run_id: uuid.UUID) -> RunStatus:
    async with sf_() as s, workspace_session_scope(s, ws):
        run = await s.get(ExecutionRun, run_id)
        assert run is not None
        return run.status


async def _to_review_ready(sf_: async_sessionmaker, ws: uuid.UUID, run_id: uuid.UUID) -> None:
    """The REAL transition. No worktree exists, so it records no local ship path — the
    shape of a run that delivers through a connector."""
    async with sf_() as s, workspace_session_scope(s, ws):
        await AgentRunner(s).transition(
            run_id=run_id, to_status=RunStatus.REVIEW_READY, reason="agent loop verified"
        )
        await s.commit()


# --- the fact ---------------------------------------------------------------------------------


async def test_a_successful_dispatch_records_that_it_delivered(sf) -> None:
    ws, _run, d_id = await _seed(sf)

    # The DeliveryWorker publishes the row's workspace around this call.
    with workspace_scope(ws):
        await dispatch_delivery(
            _Dispatcher(ok=True),
            workspace_id=ws,
            deliverable_id=d_id,
            artifact_type="weekly_report",
            session_factory=sf,
        )

    async with sf() as s, workspace_session_scope(s, ws):
        assert (await s.get(Deliverable, d_id)).delivered_at is not None  # type: ignore[union-attr]


async def test_a_failed_dispatch_records_nothing(sf) -> None:
    ws, _run, d_id = await _seed(sf)

    # The DeliveryWorker publishes the row's workspace around this call.
    with workspace_scope(ws):
        await dispatch_delivery(
            _Dispatcher(ok=False),
            workspace_id=ws,
            deliverable_id=d_id,
            artifact_type="weekly_report",
            session_factory=sf,
        )

    async with sf() as s, workspace_session_scope(s, ws):
        assert (await s.get(Deliverable, d_id)).delivered_at is None  # type: ignore[union-attr]


# --- the prod shape: delivered first, REVIEW_READY second ---------------------------------------


async def test_delivered_while_running_then_review_ready_ships(sf) -> None:
    ws, run_id, d_id = await _seed(sf)
    # The DeliveryWorker publishes the row's workspace around this call.
    with workspace_scope(ws):
        await dispatch_delivery(
            _Dispatcher(ok=True),
            workspace_id=ws,
            deliverable_id=d_id,
            artifact_type="weekly_report",
            session_factory=sf,
        )
    assert await _status(sf, ws, run_id) is RunStatus.RUNNING  # the delivery could not ship it

    await _to_review_ready(sf, ws, run_id)

    assert await _status(sf, ws, run_id) is RunStatus.SHIPPED


async def test_review_ready_with_nothing_delivered_waits(sf) -> None:
    """Control — the normal github-bound shape: its PR has not gone out yet."""
    ws, run_id, _d = await _seed(sf)

    await _to_review_ready(sf, ws, run_id)

    assert await _status(sf, ws, run_id) is RunStatus.REVIEW_READY


async def test_a_delivery_still_queued_holds_the_run(sf) -> None:
    """A partial went out, but the run's final output (its PR, say) is still in the queue —
    shipping now would mark it done before its real deliverable exists."""
    ws, run_id, _d = await _seed(sf, delivered=True)
    async with sf() as s, workspace_session_scope(s, ws):
        s.add(
            DeliveryEventRow(
                workspace_id=ws,
                deliverable_id=uuid.uuid4(),
                run_id=run_id,
                artifact_type="pr",
                payload={},
                created_at=datetime.now(tz=UTC),
            )
        )
        await s.commit()

    await _to_review_ready(sf, ws, run_id)

    assert await _status(sf, ws, run_id) is RunStatus.REVIEW_READY


async def test_an_item_awaiting_approval_holds_the_run(sf) -> None:
    ws, run_id, _d = await _seed(sf, delivered=True)
    async with sf() as s, workspace_session_scope(s, ws):
        s.add(
            SafeModeQueueItemRow(
                workspace_id=ws,
                deliverable_id=uuid.uuid4(),
                run_id=run_id,
                status=SafeModeStatus.PENDING,
                expires_at=datetime.now(tz=UTC) + timedelta(days=90),
            )
        )
        await s.commit()

    await _to_review_ready(sf, ws, run_id)

    assert await _status(sf, ws, run_id) is RunStatus.REVIEW_READY


async def test_an_item_already_decided_does_not_hold_the_run(sf) -> None:
    """Only a PENDING approval holds it — an item the founder already approved (and that went
    out) is history, not something the run still waits for."""
    ws, run_id, _d = await _seed(sf, delivered=True)
    async with sf() as s, workspace_session_scope(s, ws):
        s.add(
            SafeModeQueueItemRow(
                workspace_id=ws,
                deliverable_id=uuid.uuid4(),
                run_id=run_id,
                status=SafeModeStatus.DELIVERED,
                expires_at=datetime.now(tz=UTC) + timedelta(days=90),
            )
        )
        await s.commit()

    await _to_review_ready(sf, ws, run_id)

    assert await _status(sf, ws, run_id) is RunStatus.SHIPPED


async def test_a_retracted_delivery_does_not_count(sf) -> None:
    ws, run_id, _d = await _seed(sf, delivered=True, retracted=True)

    await _to_review_ready(sf, ws, run_id)

    assert await _status(sf, ws, run_id) is RunStatus.REVIEW_READY


# --- the sweep: whatever order, whatever race ----------------------------------------------------


async def test_the_sweep_ships_a_run_stranded_before_the_fix(sf) -> None:
    """Exactly today's run: REVIEW_READY, recorded no-local-ship, its output delivered."""
    from backend.workflow.application.run_delivery_resolution import (
        ship_delivered_review_ready_runs,
    )

    ws, run_id, _d = await _seed(
        sf,
        status=RunStatus.REVIEW_READY,
        payload={"delivers_via_local_product_repo": False},
        delivered=True,
    )
    held_ws, held_run, _ = await _seed(
        sf, status=RunStatus.REVIEW_READY, payload={"delivers_via_local_product_repo": False}
    )
    local_ws, local_run, _ = await _seed(
        sf,
        status=RunStatus.REVIEW_READY,
        payload={"delivers_via_local_product_repo": True},
        delivered=True,
    )

    async with sf() as s:
        shipped = await ship_delivered_review_ready_runs(s)

    assert shipped == 1
    assert await _status(sf, ws, run_id) is RunStatus.SHIPPED
    assert await _status(sf, held_ws, held_run) is RunStatus.REVIEW_READY  # nothing delivered
    # local auto-ship owns that one's SHIPPED hop — never raced from here
    assert await _status(sf, local_ws, local_run) is RunStatus.REVIEW_READY

    async with sf() as s:
        assert await ship_delivered_review_ready_runs(s) == 0  # idempotent


async def test_the_delivery_worker_runs_the_sweep(sf) -> None:
    from backend.workflow.infrastructure.workers.delivery_worker import DeliveryWorker

    ws, run_id, _d = await _seed(
        sf,
        status=RunStatus.REVIEW_READY,
        payload={"delivers_via_local_product_repo": False},
        delivered=True,
    )

    await DeliveryWorker(session_factory=sf, dispatcher=_Dispatcher(ok=True))._tick()  # type: ignore[arg-type]

    assert await _status(sf, ws, run_id) is RunStatus.SHIPPED
