"""런이 터미널 상태에 도달하면 그 런의 대기 결정은 런을 건드리지 않고 닫힌다."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.data.rls import workspace_session_scope
from backend.workflow.application._checkpoint_shared import ACTION_DISCARD
from backend.workflow.application.checkpoint_resolution import resolve_checkpoint
from backend.workflow.application.run_status import (
    RUN_REACHED_TERMINAL,
    close_decisions_of_terminal_runs,
    move_run_status,
)
from backend.workflow.infrastructure.db import (
    Decision,
    DecisionStatus,
    ExecutionRun,
    ExecutionRunHistory,
    RunStatus,
)

from .._support import db_engine

pytestmark = pytest.mark.asyncio

KINDS = ("ask_user_question", "run_token_cap_reached", "merge_conflict_review")


@pytest_asyncio.fixture
async def sf():
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


async def _seed(sf, status: RunStatus, kinds=KINDS, ws: uuid.UUID | None = None):
    ws, run_id = ws or uuid.uuid4(), uuid.uuid4()
    async with sf() as s, workspace_session_scope(s, ws):
        s.add(
            ExecutionRun(
                id=run_id,
                workspace_id=ws,
                status=status,
                payload={},
                created_at=datetime.now(tz=UTC),
                updated_at=datetime.now(tz=UTC),
            )
        )
        await s.flush()
        ids = []
        for kind in kinds:
            d = Decision(
                id=uuid.uuid4(),
                run_id=run_id,
                workspace_id=ws,
                decision=kind,
                payload={},
                status=DecisionStatus.PENDING,
            )
            ids.append(d.id)
            s.add(d)
        await s.commit()
    return ws, run_id, ids


async def _decisions(sf, ws, run_id):
    async with sf() as s, workspace_session_scope(s, ws):
        return list((await s.execute(select(Decision).where(Decision.run_id == run_id))).scalars())


async def test_shipping_closes_all_three_decisions_run_untouched(sf):
    ws, run_id, _ids = await _seed(sf, RunStatus.REVIEW_READY)
    async with sf() as s, workspace_session_scope(s, ws):
        run = await s.get(ExecutionRun, run_id)
        assert await move_run_status(s, run, RunStatus.SHIPPED, reason="merged")
        await s.commit()
    rows = await _decisions(sf, ws, run_id)
    assert len(rows) == 3
    assert all(d.status is DecisionStatus.RESOLVED for d in rows)
    assert all(d.resolution == RUN_REACHED_TERMINAL for d in rows)
    async with sf() as s, workspace_session_scope(s, ws):
        run = await s.get(ExecutionRun, run_id)
        assert run.status is RunStatus.SHIPPED
        history = (
            (
                await s.execute(
                    select(ExecutionRunHistory).where(ExecutionRunHistory.run_id == run_id)
                )
            )
            .scalars()
            .all()
        )
        assert [h.to_status for h in history] == [RunStatus.SHIPPED]


async def test_non_terminal_move_keeps_decisions_pending(sf):
    ws, run_id, _ids = await _seed(sf, RunStatus.RUNNING)
    async with sf() as s, workspace_session_scope(s, ws):
        run = await s.get(ExecutionRun, run_id)
        await move_run_status(s, run, RunStatus.REVIEW_READY, reason="x")
        await s.commit()
    assert all(d.status is DecisionStatus.PENDING for d in await _decisions(sf, ws, run_id))


async def test_sweep_closes_existing_decisions_of_terminal_runs(sf):
    ws, done, _ = await _seed(sf, RunStatus.SHIPPED)
    _, live, _ = await _seed(sf, RunStatus.RUNNING, ws=ws)
    async with sf() as s, workspace_session_scope(s, ws):
        assert await close_decisions_of_terminal_runs(s) == 3
        await s.commit()
    assert all(d.status is DecisionStatus.RESOLVED for d in await _decisions(sf, ws, done))
    assert all(d.status is DecisionStatus.PENDING for d in await _decisions(sf, ws, live))


async def test_discard_on_shipped_run_only_closes_decision(sf):
    ws, run_id, ids = await _seed(sf, RunStatus.SHIPPED, kinds=("verification_failed",))
    async with sf() as s, workspace_session_scope(s, ws):
        await resolve_checkpoint(
            s,
            workspace_id=ws,
            checkpoint_id=ids[0],
            action_key=ACTION_DISCARD,
            actor_id=uuid.uuid4(),
        )
        await s.commit()
    async with sf() as s, workspace_session_scope(s, ws):
        run = await s.get(ExecutionRun, run_id)
        assert run.status is RunStatus.SHIPPED
    assert (await _decisions(sf, ws, run_id))[0].status is DecisionStatus.RESOLVED


@pytest.mark.parametrize("terminal", [RunStatus.SHIPPED, RunStatus.FAILED, RunStatus.CANCELLED])
async def test_answered_on_ended_run_kinds_stay_pending(sf, terminal):
    kinds = ("delivery_failed", "merge_watch_stalled", "ask_user_question")
    ws, run_id, _ids = await _seed(sf, RunStatus.REVIEW_READY, kinds=kinds)
    async with sf() as s, workspace_session_scope(s, ws):
        run = await s.get(ExecutionRun, run_id)
        assert await move_run_status(s, run, terminal, reason="x")
        await s.commit()
    by_kind = {d.decision: d.status for d in await _decisions(sf, ws, run_id)}
    assert by_kind["delivery_failed"] is DecisionStatus.PENDING
    assert by_kind["merge_watch_stalled"] is DecisionStatus.PENDING
    assert by_kind["ask_user_question"] is DecisionStatus.RESOLVED


async def test_sweep_keeps_answered_on_ended_run_kinds_pending(sf):
    ws, run_id, _ = await _seed(
        sf, RunStatus.SHIPPED, kinds=("delivery_failed", "merge_watch_stalled")
    )
    async with sf() as s, workspace_session_scope(s, ws):
        assert await close_decisions_of_terminal_runs(s) == 0
        await s.commit()
    assert all(d.status is DecisionStatus.PENDING for d in await _decisions(sf, ws, run_id))
