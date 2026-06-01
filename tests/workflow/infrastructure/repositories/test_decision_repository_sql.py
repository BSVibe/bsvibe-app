"""Lift I-Repo-Workflow — SqlAlchemyDecisionRepository round-trip tests."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from backend.workflow.infrastructure.db import Decision, DecisionStatus, ExecutionRun, RunStatus
from backend.workflow.infrastructure.repositories import SqlAlchemyDecisionRepository
from tests._support import memory_session


async def _seed_run(session, workspace_id: uuid.UUID) -> ExecutionRun:
    run = ExecutionRun(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        status=RunStatus.RUNNING,
        payload={},
    )
    session.add(run)
    await session.flush()
    return run


@pytest.mark.asyncio
async def test_add_and_get_decision_roundtrip() -> None:
    async with memory_session() as session:
        repo = SqlAlchemyDecisionRepository(session)
        workspace_id = uuid.uuid4()
        run = await _seed_run(session, workspace_id)

        decision = Decision(
            id=uuid.uuid4(),
            run_id=run.id,
            workspace_id=workspace_id,
            decision="ask_user_question",
            payload={"question": "?"},
            rationale="needs answer",
        )
        await repo.add(decision)
        await session.flush()

        loaded = await repo.get(decision.id)
        assert loaded is not None
        assert loaded.id == decision.id
        assert loaded.status is DecisionStatus.PENDING


@pytest.mark.asyncio
async def test_list_pending_by_workspace_excludes_resolved() -> None:
    async with memory_session() as session:
        repo = SqlAlchemyDecisionRepository(session)
        workspace_id = uuid.uuid4()
        run = await _seed_run(session, workspace_id)

        pending = Decision(
            id=uuid.uuid4(),
            run_id=run.id,
            workspace_id=workspace_id,
            decision="ask_user_question",
            payload={"question": "p?"},
            status=DecisionStatus.PENDING,
        )
        resolved = Decision(
            id=uuid.uuid4(),
            run_id=run.id,
            workspace_id=workspace_id,
            decision="ask_user_question",
            payload={"question": "r?"},
            status=DecisionStatus.RESOLVED,
            resolved_at=datetime.now(tz=UTC),
            resolution="yes",
        )
        await repo.add(pending)
        await repo.add(resolved)
        await session.flush()

        rows = await repo.list_pending_by_workspace(workspace_id)
        assert [r.id for r in rows] == [pending.id]


@pytest.mark.asyncio
async def test_list_resolved_by_workspace_newest_first() -> None:
    async with memory_session() as session:
        repo = SqlAlchemyDecisionRepository(session)
        workspace_id = uuid.uuid4()
        run = await _seed_run(session, workspace_id)
        now = datetime.now(tz=UTC)

        d_old = Decision(
            id=uuid.uuid4(),
            run_id=run.id,
            workspace_id=workspace_id,
            decision="ask_user_question",
            payload={},
            status=DecisionStatus.RESOLVED,
            resolved_at=now - timedelta(hours=1),
            resolution="x",
        )
        d_new = Decision(
            id=uuid.uuid4(),
            run_id=run.id,
            workspace_id=workspace_id,
            decision="ask_user_question",
            payload={},
            status=DecisionStatus.RESOLVED,
            resolved_at=now,
            resolution="y",
        )
        await repo.add(d_old)
        await repo.add(d_new)
        await session.flush()

        rows = await repo.list_resolved_by_workspace(workspace_id)
        assert [r.id for r in rows] == [d_new.id, d_old.id]


@pytest.mark.asyncio
async def test_list_by_run_is_workspace_scoped() -> None:
    async with memory_session() as session:
        repo = SqlAlchemyDecisionRepository(session)
        workspace_id = uuid.uuid4()
        run = await _seed_run(session, workspace_id)
        await repo.add(
            Decision(
                id=uuid.uuid4(),
                run_id=run.id,
                workspace_id=workspace_id,
                decision="ask_user_question",
                payload={},
            )
        )
        await session.flush()

        # Right scope returns the row
        rows = await repo.list_by_run(run.id, workspace_id)
        assert len(rows) == 1

        # Wrong workspace returns empty — defense in depth
        empty = await repo.list_by_run(run.id, uuid.uuid4())
        assert empty == []
