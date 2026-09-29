"""[P] #959 ② — the queued-answer drain reads across tenants, writes inside one.

``drain_queued_answers`` (the agent worker's first step every tick) scans
PENDING Decisions of EVERY tenant for a chat answer the inbound layer queued,
then applies each through ``resolve_checkpoint`` — which writes the Decision and
flips its run. The probe caught both halves with an EMPTY GUC:

* the scan — under the fail-closed policy (#959 ③) it sees no Decision, and a
  founder's tap on the phone silently never lands;
* the writes — ``WITH CHECK`` wants the row's own workspace.

Two tenants, so a GUC left over from the first fails the second.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.connectors.decision_answer_queue import QUEUED_ANSWER_KEY, queue_answer
from backend.data.rls import workspace_session_scope
from backend.identity.db import MembershipRow
from backend.workflow.application.decision_answer_drain import drain_queued_answers
from backend.workflow.infrastructure.db import Decision, DecisionStatus, ExecutionRun

from .conftest import PoliciedRead, PoliciedWrite, bootstrap_tenant, requires_real_pg

pytestmark = [pytest.mark.asyncio, requires_real_pg]

_OPTIONS = ["wait", "go ahead"]


async def _tenant_with_queued_answer(factory: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    ws = await bootstrap_tenant(factory, supabase_user_id=f"q-{uuid.uuid4()}", email="q@x.io")
    async with factory() as session:
        async with workspace_session_scope(session, ws):
            owner = (
                await session.execute(
                    select(MembershipRow.user_id).where(MembershipRow.workspace_id == ws)
                )
            ).scalar_one()
            run = ExecutionRun(id=uuid.uuid4(), workspace_id=ws, status="running")
            session.add(run)
            await session.flush()
            decision = Decision(
                id=uuid.uuid4(),
                run_id=run.id,
                workspace_id=ws,
                decision="ask_user_question",
                payload={"options": _OPTIONS},
                status=DecisionStatus.PENDING,
            )
            session.add(decision)
            await session.flush()
            assert queue_answer(
                decision, action_key=None, answer=_OPTIONS[1], actor_id=owner, connector="telegram"
            )
            await session.flush()
        await session.commit()
    return ws


async def test_the_drain_reads_every_tenant_and_writes_each_in_its_own(
    session_factory: async_sessionmaker[AsyncSession],
    policied_reads: list[PoliciedRead],
    policied_writes: list[PoliciedWrite],
) -> None:
    tenants = [await _tenant_with_queued_answer(session_factory) for _ in range(2)]
    policied_reads.clear()
    policied_writes.clear()

    async with session_factory() as session:
        assert await drain_queued_answers(session) == 2

    async with session_factory() as session:
        for ws in tenants:
            async with workspace_session_scope(session, ws):
                decision = (
                    await session.execute(select(Decision).where(Decision.workspace_id == ws))
                ).scalar_one()
                assert decision.status == DecisionStatus.RESOLVED
                assert QUEUED_ANSWER_KEY not in (decision.payload or {})

    # Reads: the scan asked for every tenant by name; nothing else ran blind.
    assert any("execution_decisions" in t and g == "*" for t, g in policied_reads), policied_reads
    assert [r for r in policied_reads if r[1] == ""] == []

    # Writes: both tenants' Decisions were written (positive control), each under
    # its own workspace.
    assert {ws for table, ws, _ in policied_writes if table == "execution_decisions"} == set(
        map(str, tenants)
    )
    assert [w for w in policied_writes if w[1] != w[2]] == []
