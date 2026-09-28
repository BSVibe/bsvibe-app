"""[P] #959 ② — the tenant enumerations ask for their cross-tenant read by name.

Four background loops start by listing tenants from ``workspaces`` — an
RLS-forced table — before they scope each one:

* ``DailyBriefWorker.run_once``
* ``AuthDependencyWorker._announce``
* ``AuditRetentionSweepRunner.fire_due``
* ``SettleWorker.drain_once`` (``_resolve_workspaces``)

The 2026-09-28 probe caught all four reading it with an EMPTY GUC. Empty is
fail-open today, so they see every tenant; under the fail-closed policy (#959 ③)
the same read returns zero rows — no error, the loop just has nobody to serve.

The proposition, measured at every ORM SELECT that touches a policied table:
the transaction's GUC is never empty. And a positive control per loop — the
instrument saw that loop's ``workspaces`` read carry ``'*'`` — so a loop that
stopped enumerating cannot pass by reading nothing.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import ORMExecuteState, Session

from backend.data.rls import workspace_session_scope
from backend.knowledge.infrastructure.workers.settle_worker import Settlement, SettleWorker
from backend.shared.authz.probe import UserKeySourceStatus
from backend.workflow.infrastructure.db import ExecutionRun, ExecutionRunActivity, RunStatus
from backend.workflow.infrastructure.workers.auth_dependency_worker import AuthDependencyWorker
from backend.workflow.infrastructure.workers.daily_brief_worker import DailyBriefWorker
from plugin.audit.retention_sweep import AuditRetentionSweepRunner

from .conftest import bootstrap_tenant, requires_real_pg

pytestmark = [pytest.mark.asyncio, requires_real_pg]

_POLICIED = frozenset(
    {"workspaces", "requests", "execution_runs", "execution_decisions", "deliverables", "products"}
)

# (policied tables the SELECT touched, GUC it ran under)
_Read = tuple[frozenset[str], str]


@pytest.fixture
def policied_reads() -> Iterator[list[_Read]]:
    reads: list[_Read] = []

    def _record(state: ORMExecuteState) -> None:
        if not state.is_select:
            return
        tables = frozenset(m.local_table.name for m in state.all_mappers) & _POLICIED
        if not tables:
            return
        guc = (
            state.session.connection()
            .execute(text("SELECT current_setting('app.current_workspace_id', true)"))
            .scalar()
        )
        reads.append((tables, guc or ""))

    event.listen(Session, "do_orm_execute", _record)
    try:
        yield reads
    finally:
        event.remove(Session, "do_orm_execute", _record)


async def _two_tenants(factory: async_sessionmaker[AsyncSession]) -> list[uuid.UUID]:
    return [
        await bootstrap_tenant(factory, supabase_user_id=f"en-{uuid.uuid4()}", email=e)
        for e in ("a@x.io", "b@x.io")
    ]


def _assert_scoped_enumeration(reads: list[_Read]) -> None:
    assert any("workspaces" in t and guc == "*" for t, guc in reads), reads
    assert [r for r in reads if r[1] == ""] == []


async def test_daily_brief(
    session_factory: async_sessionmaker[AsyncSession], policied_reads: list[_Read]
) -> None:
    await _two_tenants(session_factory)
    policied_reads.clear()
    await DailyBriefWorker(session_factory=session_factory).run_once()
    _assert_scoped_enumeration(policied_reads)


async def test_auth_dependency_announce(
    session_factory: async_sessionmaker[AsyncSession], policied_reads: list[_Read]
) -> None:
    await _two_tenants(session_factory)

    async def _down() -> UserKeySourceStatus:
        return UserKeySourceStatus(ok=False, source="jwks_url", detail="unreachable")

    policied_reads.clear()
    assert (
        await AuthDependencyWorker(session_factory=session_factory, probe=_down).check_once() == 2
    )
    _assert_scoped_enumeration(policied_reads)


async def test_audit_retention_sweep(
    session_factory: async_sessionmaker[AsyncSession], policied_reads: list[_Read]
) -> None:
    await _two_tenants(session_factory)
    policied_reads.clear()
    await AuditRetentionSweepRunner().fire_due(
        session_factory=session_factory, now=datetime.now(tz=UTC)
    )
    _assert_scoped_enumeration(policied_reads)


class _NoteSink:
    def __init__(self) -> None:
        self.absorbed: list[uuid.UUID] = []

    async def absorb(self, settlement: Settlement) -> str:
        self.absorbed.append(settlement.workspace_id)
        return f"garden/{uuid.uuid4().hex}.md"


async def _seed_settle(factory: async_sessionmaker[AsyncSession], ws: uuid.UUID) -> None:
    run_id = uuid.uuid4()
    now = datetime.now(tz=UTC)
    async with factory() as session:
        async with workspace_session_scope(session, ws):
            session.add(
                ExecutionRun(
                    id=run_id,
                    workspace_id=ws,
                    status=RunStatus.REVIEW_READY,
                    payload={},
                    created_at=now,
                    updated_at=now,
                )
            )
            await session.flush()
            session.add(
                ExecutionRunActivity(
                    id=uuid.uuid4(),
                    run_id=run_id,
                    workspace_id=ws,
                    activity_type="settle",
                    payload={"verified": True, "artifact_refs": ["a.py"], "summary": "did it"},
                    created_at=now,
                )
            )
            await session.flush()
        await session.commit()


async def test_settle_resolves_policies_across_tenants(
    session_factory: async_sessionmaker[AsyncSession],
    policied_reads: list[_Read],
    tmp_path: Path,
) -> None:
    tenants = await _two_tenants(session_factory)
    for ws in tenants:
        await _seed_settle(session_factory, ws)
    sink = _NoteSink()
    policied_reads.clear()
    assert await SettleWorker(session_factory=session_factory, sink=sink).drain_once() == 2
    assert sorted(sink.absorbed) == sorted(tenants)
    _assert_scoped_enumeration(policied_reads)
