"""[P] #959 ② — the background workers write each tenant's rows under its own GUC.

A 2026-09-28 probe on the policy found two WRITES from background entry points
evaluated with an EMPTY ``app.current_workspace_id``:

* ``IntakeWorker.drain_once`` — the Request INSERT. It is added inside
  ``workspace_session_scope`` but only FLUSHED by the ``commit()`` after the
  loop, by which point the scope has cleared the GUC.
* ``AgentWorker.claim_once`` — ``open_run``'s ExecutionRun INSERT and the
  Request's RUNNING flip, with no scope at all.

Empty is fail-OPEN today, so both land. Under the fail-closed policy (#959 ③)
they are rejected — and a claim wrapped in ``cross_tenant_read()`` would carry
``'*'``, which ``WITH CHECK`` also refuses.

So the proposition is measured where it lives: at every flush that writes one
of these rows, the transaction's GUC equals THAT row's workspace. Two tenants,
so a GUC left over from the first one fails the second.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session

from backend.data.rls import workspace_session_scope
from backend.workflow.infrastructure.db import ExecutionRun, ExecutionRunHistory
from backend.workflow.infrastructure.intake.db import RequestRow, TriggerEventRow, TriggerKind
from backend.workflow.infrastructure.workers.agent_worker import AgentWorker
from backend.workflow.infrastructure.workers.intake_worker import IntakeWorker

from .conftest import bootstrap_tenant, requires_real_pg

pytestmark = [pytest.mark.asyncio, requires_real_pg]

_WATCHED = (RequestRow, ExecutionRun, ExecutionRunHistory)

# (row type, row workspace, GUC at the flush that wrote it)
_Write = tuple[str, str, str]


@pytest.fixture
def flushed_writes() -> Iterator[list[_Write]]:
    writes: list[_Write] = []

    def _record(session: Session, _ctx: Any, _instances: Any) -> None:
        rows = [o for o in (*session.new, *session.dirty) if isinstance(o, _WATCHED)]
        if not rows:
            return
        guc = (
            session.connection()
            .execute(text("SELECT current_setting('app.current_workspace_id', true)"))
            .scalar()
        )
        writes.extend((type(o).__name__, str(o.workspace_id), guc or "") for o in rows)

    event.listen(Session, "before_flush", _record)
    try:
        yield writes
    finally:
        event.remove(Session, "before_flush", _record)


async def _seed_trigger(factory: async_sessionmaker[AsyncSession], ws: uuid.UUID) -> None:
    async with factory() as session:
        async with workspace_session_scope(session, ws):
            session.add(
                TriggerEventRow(
                    id=uuid.uuid4(),
                    workspace_id=ws,
                    source="direct",
                    trigger_kind=TriggerKind.DIRECT,
                    idempotency_key=f"k-{uuid.uuid4()}",
                    payload={"text": "ship it"},
                    received_at=datetime.now(tz=UTC),
                )
            )
            await session.flush()
        await session.commit()


async def test_intake_and_claim_write_each_row_under_its_own_workspace(
    session_factory: async_sessionmaker[AsyncSession],
    flushed_writes: list[_Write],
) -> None:
    tenants = [
        await bootstrap_tenant(session_factory, supabase_user_id=f"w-{uuid.uuid4()}", email=e)
        for e in ("a@x.io", "b@x.io")
    ]
    for ws in tenants:
        await _seed_trigger(session_factory, ws)
    flushed_writes.clear()

    assert await IntakeWorker(session_factory=session_factory).drain_once() == 2
    assert await AgentWorker(session_factory=session_factory).claim_once() == 2

    # Positive control: the instrument saw every write this proposition is about.
    written = {(kind, ws) for kind, ws, _ in flushed_writes}
    for ws in map(str, tenants):
        assert {("RequestRow", ws), ("ExecutionRun", ws), ("ExecutionRunHistory", ws)} <= written

    blind = [w for w in flushed_writes if w[1] != w[2]]
    assert blind == []
