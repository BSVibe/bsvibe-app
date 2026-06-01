"""Lift J — Multi-server safety hardening (v8 §11.5).

Each worker advance/claim path MUST be claim-or-skip safe so a second
instance running the same DB cannot double-fire the same row. The pre-
Lift-J audit found three gaps:

* :class:`~backend.workflow.infrastructure.workers.delivery_worker.DeliveryWorker`
  — drained ``delivery_events`` with an unlocked SELECT.
* :class:`~backend.knowledge.infrastructure.workers.settle_worker.SettleWorker`
  — drained ``execution_run_activities`` with an unlocked SELECT, AND
  ran the per-workspace promoter with no mutex (two servers could
  promote the same workspace concurrently).
* :class:`~backend.workflow.infrastructure.workers.relay_worker.RelayWorker`
  — drained ``audit_outbox`` with an unlocked SELECT.

This module asserts the invariants in two ways:

1. **Compile-time** — the SELECT statement carries ``FOR UPDATE SKIP
   LOCKED`` in its rendered SQL. This is the load-bearing check that
   protects production PG. A regression that drops the lock hint fails
   here at unit-test speed.
2. **Behavioural** — for the workspace-promote site (per-workspace
   advisory lock), two concurrent callers on the same workspace
   produce exactly one acquire + one busy. The SQLite fallback path in
   :mod:`backend.workflow.infrastructure.lease` makes this meaningful
   without a real PG.

The row-claim sites (delivery / settle / relay) also have behavioural
PG-only race tests — ``SKIP LOCKED`` is a PG primitive; SQLite ignores
the hint at the dialect level, so a behavioural race test there would
be measuring the wrong substrate. The compile-time guard plus the PG
race tests cover both layers.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.workflow.domain.delivery import ActionResult, DeliveryResult
from backend.workflow.infrastructure.db import ExecutionBase
from backend.workflow.infrastructure.delivery.db import DeliveryBase, DeliveryEventRow
from backend.workflow.infrastructure.workers.delivery_worker import (
    DeliveryWorker,
    DeliveryWorkerConfig,
)
from backend.workflow.infrastructure.workers.relay_worker import RelayConfig, RelayWorker
from plugin.audit.models import AuditOutboxBase, AuditOutboxRecord
from plugin.audit.store import OutboxStore
from tests._support import db_engine, use_real_pg

# ----------------------------------------------------------------------
# Compile-time assertions — every claim/drain statement carries the lock
# hint. This is the load-bearing protection in production: an unrelated
# refactor that silently drops ``with_for_update`` is caught at unit-
# test speed, not on the day the second uvicorn ships.
# ----------------------------------------------------------------------


def _rendered_sql(stmt: Any) -> str:
    """Render a statement against the PG dialect.

    ``SKIP LOCKED`` is a PG dialect extension — the default compile
    (generic SQL) omits it. We MUST compile against the PG dialect to
    assert the lock hint is wired through to production.
    """
    from sqlalchemy.dialects import postgresql

    return str(
        stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": False})
    ).upper()


def test_delivery_event_select_carries_skip_locked() -> None:
    """Lift J — DeliveryWorker drain must claim-or-skip on PG."""
    from backend.workflow.infrastructure.workers.delivery_worker import (
        build_delivery_claim_stmt,
    )

    sql = _rendered_sql(build_delivery_claim_stmt(batch_size=10))
    assert "FOR UPDATE" in sql, "DeliveryEventRow drain must FOR UPDATE"
    assert "SKIP LOCKED" in sql, "DeliveryEventRow drain must SKIP LOCKED"


def test_settle_activity_select_carries_skip_locked() -> None:
    """Lift J — SettleWorker drain must claim-or-skip on PG."""
    from backend.knowledge.infrastructure.workers.settle_worker import (
        build_settle_claim_stmt,
    )

    sql = _rendered_sql(build_settle_claim_stmt(batch_size=10))
    assert "FOR UPDATE" in sql, "ExecutionRunActivity drain must FOR UPDATE"
    assert "SKIP LOCKED" in sql, "ExecutionRunActivity drain must SKIP LOCKED"


def test_outbox_select_undelivered_carries_skip_locked() -> None:
    """Lift J — RelayWorker outbox drain must claim-or-skip on PG."""
    store = OutboxStore()
    stmt = store.build_select_undelivered_stmt(batch_size=10, now=datetime.now(tz=UTC))
    sql = _rendered_sql(stmt)
    assert "FOR UPDATE" in sql, "audit_outbox drain must FOR UPDATE"
    assert "SKIP LOCKED" in sql, "audit_outbox drain must SKIP LOCKED"


# ----------------------------------------------------------------------
# Workspace promote-lease — per-workspace advisory lock so two servers
# don't concurrently run the same workspace's promoter. SQLite fallback
# makes this a real behavioural check at unit-test speed.
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_workspace_promote_lock_first_acquires_second_busy() -> None:
    """Two concurrent callers on the same workspace — only one wins."""
    from sqlalchemy.ext.asyncio import create_async_engine

    from backend.workflow.infrastructure.lease import (
        release_workspace_promote_lock,
        try_workspace_promote_lock,
    )

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.connect() as conn:
            session = AsyncSession(bind=conn)
            ws_id = uuid.uuid4()
            first = await try_workspace_promote_lock(session, ws_id)
            assert first is True

            async def second_call() -> bool:
                return await try_workspace_promote_lock(session, ws_id)

            assert await asyncio.create_task(second_call()) is False

            await release_workspace_promote_lock(session, ws_id)
            # Re-acquire after release is fine — idempotent.
            again = await try_workspace_promote_lock(session, ws_id)
            assert again is True
            await release_workspace_promote_lock(session, ws_id)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_workspace_promote_lock_disjoint_workspaces_independent() -> None:
    """Different workspaces share no lease — both acquire concurrently."""
    from sqlalchemy.ext.asyncio import create_async_engine

    from backend.workflow.infrastructure.lease import (
        release_workspace_promote_lock,
        try_workspace_promote_lock,
    )

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.connect() as conn:
            session = AsyncSession(bind=conn)
            ws_a = uuid.uuid4()
            ws_b = uuid.uuid4()
            assert await try_workspace_promote_lock(session, ws_a) is True
            assert await try_workspace_promote_lock(session, ws_b) is True
            await release_workspace_promote_lock(session, ws_a)
            await release_workspace_promote_lock(session, ws_b)
    finally:
        await engine.dispose()


# ----------------------------------------------------------------------
# Behavioural row-claim race tests — PG-only (SKIP LOCKED is a PG
# primitive; SQLite ignores the hint at the dialect level). These prove
# the lock hint actually changes runtime behaviour on production PG.
# ----------------------------------------------------------------------


class _CaptureDispatcher:
    def __init__(self) -> None:
        self.calls: list[uuid.UUID] = []

    async def dispatch(self, **kwargs: object) -> DeliveryResult:
        did = kwargs["deliverable_id"]
        assert isinstance(did, uuid.UUID)
        self.calls.append(did)
        # Tiny sleep widens the race window so worker_b's drain query
        # actually overlaps worker_a's lock.
        await asyncio.sleep(0.02)
        return DeliveryResult(
            workspace_id=kwargs["workspace_id"],  # type: ignore[arg-type]
            deliverable_id=did,
            artifact_type=str(kwargs["artifact_type"]),
            actions=[ActionResult(action="noop", succeeded=True)],
            delivered_at=datetime.now(tz=UTC),
        )


@pytest.mark.skipif(
    not use_real_pg(),
    reason="SKIP LOCKED is a PG-only primitive; SQLite ignores the hint",
)
@pytest.mark.asyncio
async def test_two_delivery_workers_race_no_double_dispatch_pg() -> None:
    """Two DeliveryWorker instances on real PG → each row dispatched exactly once."""
    async with db_engine(DeliveryBase, ExecutionBase) as (engine, _is_pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        ws = uuid.uuid4()
        deliv_ids = [uuid.uuid4() for _ in range(6)]
        async with sf() as s:
            for did in deliv_ids:
                s.add(
                    DeliveryEventRow(
                        id=uuid.uuid4(),
                        workspace_id=ws,
                        deliverable_id=did,
                        artifact_type="pr",
                        payload={},
                        created_at=datetime.now(tz=UTC),
                    )
                )
            await s.commit()

        dispatcher = _CaptureDispatcher()
        cfg = DeliveryWorkerConfig(batch_size=10, poll_interval_s=0.01)
        worker_a = DeliveryWorker(session_factory=sf, dispatcher=dispatcher, config=cfg)
        worker_b = DeliveryWorker(session_factory=sf, dispatcher=dispatcher, config=cfg)

        results = await asyncio.gather(
            worker_a.drain_once(),
            worker_b.drain_once(),
            return_exceptions=True,
        )
        for r in results:
            assert not isinstance(r, BaseException), f"worker raised: {r!r}"

        called = sorted(str(d) for d in dispatcher.calls)
        assert len(called) == len(set(called)), f"double dispatch: {called!r}"
        async with sf() as s:
            remaining = (await s.execute(select(DeliveryEventRow))).scalars().all()
            assert remaining == []


class _CaptureRelay:
    def __init__(self) -> None:
        self.seen: list[int] = []

    async def send(self, records):  # type: ignore[no-untyped-def]
        ids = [r.id for r in records]
        self.seen.extend(ids)
        await asyncio.sleep(0.02)
        return ids


@pytest.mark.skipif(
    not use_real_pg(),
    reason="SKIP LOCKED is a PG-only primitive; SQLite ignores the hint",
)
@pytest.mark.asyncio
async def test_two_relay_workers_race_no_double_relay_pg() -> None:
    """Two RelayWorker instances on real PG → each outbox id sent exactly once."""
    async with db_engine(AuditOutboxBase) as (engine, _is_pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        async with sf() as s:
            for i in range(6):
                s.add(
                    AuditOutboxRecord(
                        event_id=f"evt-{i}",
                        event_type="gateway.completion.dispatched",
                        occurred_at=datetime.now(tz=UTC),
                        payload={"i": i},
                    )
                )
            await s.commit()

        relay = _CaptureRelay()
        worker_a = RelayWorker(
            session_factory=sf,
            relay=relay,
            config=RelayConfig(batch_size=10, poll_interval_s=0.01),
        )
        worker_b = RelayWorker(
            session_factory=sf,
            relay=relay,
            config=RelayConfig(batch_size=10, poll_interval_s=0.01),
        )

        results = await asyncio.gather(
            worker_a.drain_once(),
            worker_b.drain_once(),
            return_exceptions=True,
        )
        for r in results:
            assert not isinstance(r, BaseException), f"worker raised: {r!r}"

        assert len(relay.seen) == len(set(relay.seen)), f"double relay: {relay.seen!r}"
