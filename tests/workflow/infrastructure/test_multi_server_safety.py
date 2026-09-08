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
be measuring the wrong substrate.

Those two layers were claimed to cover each other, and until 2026-09-08
they did not. Measured by mutation: dropping ``skip_locked=True`` while
keeping ``FOR UPDATE`` left BOTH behavioural race tests green and only
the compile-time guards red. The reason is that the two primitives do
not differ in the OUTCOME these tests were checking — with a plain
``FOR UPDATE`` the sibling simply BLOCKS until the first worker commits,
then reads rows that are already gone, and still dispatches nothing
twice. What ``SKIP LOCKED`` buys is that the sibling comes back at all
while the lock is held, and nothing was measuring that.

So each race test now holds the first worker inside its transaction
(``_Hold``, an event handshake rather than a sleep — see #899 for what a
sleep-bought window is actually worth) and asserts the sibling returned
while that lock was held. The same mutation now fails both layers.
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


#: How long the holding worker waits for its sibling before giving up on it.
#: This is a LIVENESS bound, not a race window — with ``SKIP LOCKED`` the
#: sibling comes back in milliseconds, and without it the sibling is blocked on
#: a lock that is not going to be released until this worker commits. Generous
#: on purpose: a slow runner must not turn "the lock works" into a failure.
_SIBLING_DEADLINE_S = 15.0


class _Hold:
    """A deterministic "worker A is mid-batch, holding its lock" state.

    Replaces ``asyncio.sleep(0.02)``. A sleep only *probably* overlaps the
    sibling's claim, and measured on real PG the margin such a window leaves is
    a few milliseconds — see #899, where exactly that shape made
    ``test_two_concurrent_claims_no_double_claim_pg`` flake on a loaded runner
    and abandoned PR #892.
    """

    def __init__(self) -> None:
        self.holding = asyncio.Event()
        self.sibling_done = asyncio.Event()
        self.sibling_blocked = False

    async def hold(self) -> None:
        """Called from inside worker A's open transaction."""
        self.holding.set()
        try:
            await asyncio.wait_for(self.sibling_done.wait(), timeout=_SIBLING_DEADLINE_S)
        except TimeoutError:
            # The sibling never came back. It is blocked on OUR lock — which is
            # precisely what ``SKIP LOCKED`` exists to prevent. Record it and
            # commit anyway, so the sibling unblocks and the test reports the
            # real reason instead of hanging.
            self.sibling_blocked = True


class _CaptureDispatcher:
    def __init__(self, hold: _Hold | None = None) -> None:
        self.calls: list[uuid.UUID] = []
        self._hold = hold

    async def dispatch(self, **kwargs: object) -> DeliveryResult:
        did = kwargs["deliverable_id"]
        assert isinstance(did, uuid.UUID)
        self.calls.append(did)
        if self._hold is not None:
            await self._hold.hold()
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
    """Two DeliveryWorker instances on real PG → each row dispatched exactly once,
    and the second one is never made to WAIT for the first.

    The second half is the part ``SKIP LOCKED`` actually buys, and until
    2026-09-08 nothing measured it. Measured then, by mutation: dropping
    ``skip_locked=True`` (keeping ``FOR UPDATE``) left this test GREEN — the
    sibling simply blocked until the first worker committed, found the rows
    deleted, and no row was dispatched twice. Only the compile-time SQL guard
    went red. So the module docstring's claim that the two layers are both
    covered was false, and the difference between the primitives is not in the
    OUTCOME at all — it is whether the sibling comes back while the lock is
    held. That is what ``_Hold`` asserts."""
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

        # HALF the rows each, so there is something left for the sibling to
        # claim while the first worker is still holding its own batch. With
        # batch_size >= the row count the first worker takes everything and the
        # sibling has nothing to prove.
        hold = _Hold()
        dispatcher_a = _CaptureDispatcher(hold)
        dispatcher_b = _CaptureDispatcher()
        cfg = DeliveryWorkerConfig(batch_size=3, poll_interval_s=0.01)
        worker_a = DeliveryWorker(session_factory=sf, dispatcher=dispatcher_a, config=cfg)
        worker_b = DeliveryWorker(session_factory=sf, dispatcher=dispatcher_b, config=cfg)

        async def _sibling() -> int:
            # Start only once worker A is provably inside its transaction with
            # rows locked — no sleep, no window to lose on a slow runner.
            await asyncio.wait_for(hold.holding.wait(), timeout=_SIBLING_DEADLINE_S)
            try:
                return await worker_b.drain_once()
            finally:
                hold.sibling_done.set()

        results = await asyncio.gather(worker_a.drain_once(), _sibling(), return_exceptions=True)
        for r in results:
            assert not isinstance(r, BaseException), f"worker raised: {r!r}"

        assert not hold.sibling_blocked, (
            "the sibling waited out the first worker's lock instead of skipping it — "
            "SKIP LOCKED is not in effect on the delivery claim"
        )
        assert dispatcher_b.calls, "the sibling must have claimed the rows A did not"
        assert not set(dispatcher_a.calls) & set(dispatcher_b.calls), (
            f"double dispatch: {dispatcher_a.calls!r} & {dispatcher_b.calls!r}"
        )
        called = sorted(str(d) for d in [*dispatcher_a.calls, *dispatcher_b.calls])
        assert len(called) == len(set(called)), f"double dispatch: {called!r}"
        assert called == sorted(str(d) for d in deliv_ids), "every row delivered exactly once"
        async with sf() as s:
            remaining = (await s.execute(select(DeliveryEventRow))).scalars().all()
            assert remaining == []


class _CaptureRelay:
    def __init__(self, hold: _Hold | None = None) -> None:
        self.seen: list[int] = []
        self._hold = hold

    async def send(self, records):  # type: ignore[no-untyped-def]
        ids = [r.id for r in records]
        self.seen.extend(ids)
        if self._hold is not None:
            await self._hold.hold()
        return ids


@pytest.mark.skipif(
    not use_real_pg(),
    reason="SKIP LOCKED is a PG-only primitive; SQLite ignores the hint",
)
@pytest.mark.asyncio
async def test_two_relay_workers_race_no_double_relay_pg() -> None:
    """Two RelayWorker instances on real PG → each outbox id sent exactly once,
    and the second one is never made to WAIT for the first.

    Same measurement as the delivery race above: dropping ``skip_locked=True``
    from the outbox claim left the old version of this test green, because the
    sibling blocked, then read rows the first worker had already marked
    delivered. Nothing was relayed twice either way — the primitives differ in
    whether the sibling comes back, not in the outcome."""
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

        # Half the batch each, so the sibling has rows left to claim while the
        # first worker still holds its own (see the delivery race above).
        hold = _Hold()
        relay_a = _CaptureRelay(hold)
        relay_b = _CaptureRelay()
        cfg = RelayConfig(batch_size=3, poll_interval_s=0.01)
        worker_a = RelayWorker(session_factory=sf, relay=relay_a, config=cfg)
        worker_b = RelayWorker(session_factory=sf, relay=relay_b, config=cfg)

        async def _sibling() -> int:
            await asyncio.wait_for(hold.holding.wait(), timeout=_SIBLING_DEADLINE_S)
            try:
                return await worker_b.drain_once()
            finally:
                hold.sibling_done.set()

        results = await asyncio.gather(worker_a.drain_once(), _sibling(), return_exceptions=True)
        for r in results:
            assert not isinstance(r, BaseException), f"worker raised: {r!r}"

        assert not hold.sibling_blocked, (
            "the sibling waited out the first worker's lock instead of skipping it — "
            "SKIP LOCKED is not in effect on the audit-outbox claim"
        )
        assert relay_b.seen, "the sibling must have claimed the rows A did not"
        assert not set(relay_a.seen) & set(relay_b.seen), (
            f"double relay: {relay_a.seen!r} & {relay_b.seen!r}"
        )
        seen = [*relay_a.seen, *relay_b.seen]
        assert len(seen) == len(set(seen)), f"double relay: {seen!r}"
