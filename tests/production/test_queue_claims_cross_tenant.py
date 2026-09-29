"""[P] #959 ② — the queue claims read across tenants and write inside one.

The last three blind sites of the probe are the agent worker's queue claims:

* ``claim_once`` — ``SELECT … FOR UPDATE SKIP LOCKED`` over OPEN requests
  (its writes were scoped by #1067; the read was not)
* ``_claim_runs_for_drive`` — ``UPDATE … WHERE id IN (SELECT … SKIP LOCKED)``
* ``_reap_stale_claims`` — ``UPDATE … WHERE claimed_at < lease``

A single cross-tenant UPDATE cannot survive the policy: ``'*'`` opens ``USING``
only, and ``WITH CHECK`` wants the row's own workspace. So each claim becomes,
in ONE transaction: lock the candidates with ``'*'``, then update each row under
its own tenant. The locks are held to the commit, so ``SKIP LOCKED`` keeps its
multi-worker meaning.

Measured per statement: no policied SELECT runs blind; no statement-level
UPDATE runs blind or under ``'*'``; and the outcome — every tenant's row was
claimed — which a WRONG tenant's GUC (fail-closed today) would break.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.config import get_settings
from backend.data.rls import cross_tenant_session_read, workspace_session_scope
from backend.workflow.infrastructure.db import ExecutionRun, RunStatus
from backend.workflow.infrastructure.intake.db import (
    RequestRow,
    RequestStatus,
    TriggerEventRow,
    TriggerKind,
)
from backend.workflow.infrastructure.workers.agent_worker import AgentWorker

from .conftest import (
    PoliciedRead,
    PoliciedUpdate,
    PoliciedWrite,
    bootstrap_tenant,
    requires_real_pg,
)

pytestmark = [pytest.mark.asyncio, requires_real_pg]

_NOW = datetime.now(tz=UTC)


async def _two_tenants(factory: async_sessionmaker[AsyncSession]) -> list[uuid.UUID]:
    return [
        await bootstrap_tenant(factory, supabase_user_id=f"c-{uuid.uuid4()}", email=e)
        for e in ("a@x.io", "b@x.io")
    ]


async def _seed_run(
    factory: async_sessionmaker[AsyncSession],
    ws: uuid.UUID,
    *,
    status: RunStatus,
    claimed_at: datetime | None = None,
) -> uuid.UUID:
    run_id = uuid.uuid4()
    async with factory() as session:
        async with workspace_session_scope(session, ws):
            session.add(
                ExecutionRun(
                    id=run_id,
                    workspace_id=ws,
                    status=status,
                    claimed_at=claimed_at,
                    claimed_by=uuid.uuid4() if claimed_at else None,
                    payload={},
                    created_at=_NOW,
                    updated_at=_NOW,
                )
            )
            await session.flush()
        await session.commit()
    return run_id


async def _seed_request(factory: async_sessionmaker[AsyncSession], ws: uuid.UUID) -> None:
    async with factory() as session:
        async with workspace_session_scope(session, ws):
            trig = TriggerEventRow(
                id=uuid.uuid4(),
                workspace_id=ws,
                source="direct",
                trigger_kind=TriggerKind.DIRECT,
                idempotency_key=f"k-{uuid.uuid4()}",
                payload={},
                received_at=_NOW,
            )
            session.add(trig)
            await session.flush()
            session.add(
                RequestRow(
                    id=uuid.uuid4(),
                    workspace_id=ws,
                    trigger_event_id=trig.id,
                    status=RequestStatus.OPEN,
                    payload={"text": "hi"},
                    created_at=_NOW,
                    updated_at=_NOW,
                )
            )
            await session.flush()
        await session.commit()


async def _statuses(
    factory: async_sessionmaker[AsyncSession], ids: list[uuid.UUID]
) -> set[RunStatus]:
    async with factory() as session:
        async with cross_tenant_session_read(session):
            rows = (
                await session.execute(
                    select(ExecutionRun.workspace_id, ExecutionRun.id).where(
                        ExecutionRun.id.in_(ids)
                    )
                )
            ).all()
        out: set[RunStatus] = set()
        for ws, rid in rows:
            async with workspace_session_scope(session, ws):
                run = await session.get(ExecutionRun, rid)
                assert run is not None
                out.add(run.status)
        return out


def _assert_no_blind(reads: list[PoliciedRead], table: str) -> None:
    assert any(table in t and g == "*" for t, g in reads), reads
    assert [r for r in reads if r[1] == ""] == []


def _assert_tenant_updates(updates: list[PoliciedUpdate], tenants: list[uuid.UUID]) -> None:
    assert {g for _, g in updates} == set(map(str, tenants)), updates


async def test_claim_once(
    session_factory: async_sessionmaker[AsyncSession],
    policied_reads: list[PoliciedRead],
    policied_writes: list[PoliciedWrite],
) -> None:
    tenants = await _two_tenants(session_factory)
    for ws in tenants:
        await _seed_request(session_factory, ws)
    policied_reads.clear()
    policied_writes.clear()

    assert await AgentWorker(session_factory=session_factory).claim_once() == 2

    _assert_no_blind(policied_reads, "requests")
    assert {ws for t, ws, _ in policied_writes if t == "requests"} == set(map(str, tenants))
    assert [w for w in policied_writes if w[1] != w[2]] == []


async def test_claim_runs_for_drive(
    session_factory: async_sessionmaker[AsyncSession],
    policied_reads: list[PoliciedRead],
    policied_updates: list[PoliciedUpdate],
) -> None:
    tenants = await _two_tenants(session_factory)
    run_ids = [await _seed_run(session_factory, ws, status=RunStatus.OPEN) for ws in tenants]
    policied_reads.clear()
    policied_updates.clear()

    claimed = await AgentWorker(session_factory=session_factory)._claim_runs_for_drive()
    reads, updates = list(policied_reads), list(policied_updates)

    assert sorted(claimed) == sorted(zip(run_ids, tenants, strict=True))
    assert await _statuses(session_factory, run_ids) == {RunStatus.RUNNING}
    _assert_no_blind(reads, "execution_runs")
    _assert_tenant_updates(updates, tenants)


async def test_reap_stale_claims(
    session_factory: async_sessionmaker[AsyncSession],
    policied_reads: list[PoliciedRead],
    policied_updates: list[PoliciedUpdate],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "executor_task_timeout_s", 1.0, raising=False)
    tenants = await _two_tenants(session_factory)
    stale = _NOW - timedelta(minutes=5)
    run_ids = [
        await _seed_run(session_factory, ws, status=RunStatus.RUNNING, claimed_at=stale)
        for ws in tenants
    ]
    policied_reads.clear()
    policied_updates.clear()

    assert await AgentWorker(session_factory=session_factory)._reap_stale_claims() == 2
    reads, updates = list(policied_reads), list(policied_updates)

    assert await _statuses(session_factory, run_ids) == {RunStatus.OPEN}
    _assert_no_blind(reads, "execution_runs")
    _assert_tenant_updates(updates, tenants)


# ---------------------------------------------------------------------------
# SKIP LOCKED survived the reshape: a row another session holds is skipped, not
# waited on. Two concurrent claims ending disjoint cannot show this — with a
# plain FOR UPDATE the sibling just blocks, then finds the rows gone.
# ---------------------------------------------------------------------------

#: Liveness bound — with SKIP LOCKED the claim returns in milliseconds; without
#: it, it blocks on a lock this test only releases after the bound.
_SIBLING_DEADLINE_S = 10.0


async def _hold_lock(
    factory: async_sessionmaker[AsyncSession], table: str, row_id: uuid.UUID
) -> AsyncSession:
    session = factory()
    await session.execute(text("SELECT set_config('app.current_workspace_id', '*', true)"))
    await session.execute(text(f"SELECT id FROM {table} WHERE id = :id FOR UPDATE"), {"id": row_id})
    return session


@pytest.mark.parametrize("kind", ["runs", "stale", "requests"])
async def test_a_row_held_by_another_session_is_skipped(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    monkeypatch.setattr(get_settings(), "executor_task_timeout_s", 1.0, raising=False)
    tenants = await _two_tenants(session_factory)
    worker = AgentWorker(session_factory=session_factory)

    if kind == "requests":
        for ws in tenants:
            await _seed_request(session_factory, ws)
        async with session_factory() as s, cross_tenant_session_read(s):
            held_id = (
                await s.execute(text("SELECT id FROM requests ORDER BY created_at LIMIT 1"))
            ).scalar_one()
        table, claim = "requests", worker.claim_once
    else:
        status = RunStatus.OPEN if kind == "runs" else RunStatus.RUNNING
        claimed_at = None if kind == "runs" else _NOW - timedelta(minutes=5)
        ids = [
            await _seed_run(session_factory, ws, status=status, claimed_at=claimed_at)
            for ws in tenants
        ]
        held_id = ids[0]
        table = "execution_runs"
        claim = worker._claim_runs_for_drive if kind == "runs" else worker._reap_stale_claims

    holder = await _hold_lock(session_factory, table, held_id)
    try:
        got = await asyncio.wait_for(claim(), timeout=_SIBLING_DEADLINE_S)
    finally:
        await holder.rollback()
        await holder.close()

    count = got if isinstance(got, int) else len(got)
    assert count == 1, f"expected the unheld row only, got {got!r}"
