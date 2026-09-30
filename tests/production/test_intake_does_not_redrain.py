"""[P] #959 — a drained trigger is never drained again, whatever the GUC.

2026-09-29 incident: minutes after the RLS policy went fail-closed (#1091) the
IntakeWorker re-drained 50 old triggers every tick — 1000 duplicate requests,
10 runs. ``list_undrained`` decides "already drained" with
``NOT EXISTS (requests …)``; the scan runs with no workspace, ``requests`` is
RLS-forced, so the EXISTS saw nothing and every trigger read as new. A blind
read inside a NEGATION does not go quiet — it acts.

The engine below pins a FOREIGN workspace on every checkout: any statement the
worker sends without asking for a tenant (``'*'`` or a scope) sees no
``requests`` row — exactly what fail-closed does to a blind read — while this
suite's policy stays open for everyone else.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.data.rls import workspace_session_scope
from backend.workflow.infrastructure.intake.db import (
    RequestRow,
    RequestStatus,
    TriggerEventRow,
    TriggerKind,
)
from backend.workflow.infrastructure.workers.intake_worker import IntakeWorker

from .._support import pg_url
from .conftest import bootstrap_tenant, requires_real_pg

pytestmark = [pytest.mark.asyncio, requires_real_pg]


async def _drained_trigger(factory: async_sessionmaker[AsyncSession], ws: uuid.UUID) -> None:
    now = datetime.now(tz=UTC)
    async with factory() as session:
        async with workspace_session_scope(session, ws):
            trig = TriggerEventRow(
                id=uuid.uuid4(),
                workspace_id=ws,
                source="direct",
                trigger_kind=TriggerKind.DIRECT,
                idempotency_key=f"k-{uuid.uuid4()}",
                payload={"text": "old work"},
                received_at=now,
            )
            session.add(trig)
            await session.flush()
            session.add(
                RequestRow(
                    id=uuid.uuid4(),
                    workspace_id=ws,
                    trigger_event_id=trig.id,
                    status=RequestStatus.SHIPPED,
                    payload={"text": "old work"},
                    created_at=now,
                    updated_at=now,
                )
            )
            await session.flush()
        await session.commit()


async def test_a_drained_trigger_is_not_drained_again_when_blind_reads_see_nothing(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    ws = await bootstrap_tenant(
        session_factory, supabase_user_id=f"rd-{uuid.uuid4()}", email="r@x.io"
    )
    await _drained_trigger(session_factory, ws)

    blinkered = create_async_engine(pg_url(), future=True)

    @event.listens_for(blinkered.sync_engine, "checkout")
    def _foreign(dbapi_conn, _record, _proxy):  # type: ignore[no-untyped-def]
        cur = dbapi_conn.cursor()
        cur.execute(f"SET app.current_workspace_id = '{uuid.uuid4()}'")
        cur.close()

    try:
        worker = IntakeWorker(session_factory=async_sessionmaker(blinkered, expire_on_commit=False))
        assert await worker.drain_once() == 0
        assert await worker.drain_once() == 0
    finally:
        await blinkered.dispose()

    async with session_factory() as session:
        async with workspace_session_scope(session, ws):
            count = (
                await session.execute(select(func.count()).select_from(RequestRow))
            ).scalar_one()
    assert count == 1, "the drained trigger grew duplicate requests"
