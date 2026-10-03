"""#1102 — a run cancelled by ANOTHER session must not be overwritten by the loop.

Prod 2026-09-30, run ``5044c88a``: cancelled at 08:36, then at 08:37:51 the
history gained ``running → review_ready`` — note ``from_status`` is ``running``,
not ``cancelled``. The writer never saw the cancel.

``AgentRunner.transition`` guarded on ``run.status`` as read through
``session.get``, which returns the object already in the session's identity map.
The drive loop's session loads the run at the start and commits at every turn
boundary with ``expire_on_commit=False`` (runtime/lifecycle.py), so that object
still says ``running`` long after the founder's request committed ``cancelled``
from a different session. The guard passed, and the ORM flush is an
unconditional ``UPDATE … SET status`` — it wrote over the cancel.

The fix makes the DATABASE decide: the transition is a compare-and-set
(``UPDATE … WHERE id = :id AND status = :from``). Zero rows means someone else
moved the run first; the transition is a no-op.

The sessions here are separate sessions on one engine — the same shape as prod
(the loop's session vs. the API request's session).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.data.rls import workspace_session_scope
from backend.workflow.application.agent_runner import AgentRunner
from backend.workflow.infrastructure.db import ExecutionRun, ExecutionRunHistory, RunStatus

from .._support import db_engine

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def sf():
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


async def _seed(sf_, workspace_id: uuid.UUID) -> uuid.UUID:
    async with sf_() as s, workspace_session_scope(s, workspace_id):
        run = ExecutionRun(
            id=uuid.uuid4(),
            workspace_id=workspace_id,
            status=RunStatus.RUNNING,
            payload={"text": "fix the race"},
            created_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC),
        )
        s.add(run)
        await s.commit()
        return run.id


async def _status(sf_, workspace_id: uuid.UUID, run_id: uuid.UUID) -> RunStatus:
    async with sf_() as s, workspace_session_scope(s, workspace_id):
        row = (
            await s.execute(select(ExecutionRun.status).where(ExecutionRun.id == run_id))
        ).scalar_one()
        return row


async def _cancel_elsewhere(sf_, workspace_id: uuid.UUID, run_id: uuid.UUID) -> None:
    """The founder's cancel, committed by a DIFFERENT session (the API request)."""
    async with sf_() as s, workspace_session_scope(s, workspace_id):
        assert await AgentRunner(s).transition(
            run_id=run_id, to_status=RunStatus.CANCELLED, reason="founder"
        )
        await s.commit()


@pytest.mark.parametrize("to_status", [RunStatus.REVIEW_READY, RunStatus.FAILED])
async def test_a_cancel_committed_elsewhere_survives_the_loops_terminal_write(
    sf, to_status: RunStatus
) -> None:
    workspace_id = uuid.uuid4()
    run_id = await _seed(sf, workspace_id)

    async with sf() as loop, workspace_session_scope(loop, workspace_id):
        # The drive loop holds the run in its identity map …
        held = await loop.get(ExecutionRun, run_id)
        assert held is not None and held.status is RunStatus.RUNNING
        # … and commits at a turn boundary (expire_on_commit=False keeps it stale).
        await loop.commit()

        await _cancel_elsewhere(sf, workspace_id, run_id)

        moved = await AgentRunner(loop).transition(run_id=run_id, to_status=to_status)
        await loop.commit()

    assert moved is False
    assert await _status(sf, workspace_id, run_id) is RunStatus.CANCELLED


async def test_no_history_row_records_a_transition_that_did_not_happen(sf) -> None:
    """The history is the record #1102 was diagnosed from — it must not claim a
    ``running → review_ready`` that the database refused."""
    workspace_id = uuid.uuid4()
    run_id = await _seed(sf, workspace_id)

    async with sf() as loop, workspace_session_scope(loop, workspace_id):
        # HOLD the reference, as the drive loop does. The identity map is weak: a
        # discarded ``get`` result is collected and the next ``get`` reloads from
        # the DB — which hides the race (this test was green before the fix).
        held = await loop.get(ExecutionRun, run_id)
        assert held is not None
        await loop.commit()
        await _cancel_elsewhere(sf, workspace_id, run_id)
        await AgentRunner(loop).transition(run_id=run_id, to_status=RunStatus.REVIEW_READY)
        await loop.commit()

    async with sf() as s, workspace_session_scope(s, workspace_id):
        rows = (
            await s.execute(
                select(ExecutionRunHistory.to_status).where(ExecutionRunHistory.run_id == run_id)
            )
        ).scalars()
        assert RunStatus.REVIEW_READY not in list(rows)


async def test_an_uncontested_transition_still_moves(sf) -> None:
    """Control — with no one else writing, the loop's transition lands."""
    workspace_id = uuid.uuid4()
    run_id = await _seed(sf, workspace_id)

    async with sf() as loop, workspace_session_scope(loop, workspace_id):
        held = await loop.get(ExecutionRun, run_id)
        assert held is not None
        await loop.commit()
        moved = await AgentRunner(loop).transition(run_id=run_id, to_status=RunStatus.REVIEW_READY)
        await loop.commit()

    assert moved is True
    assert await _status(sf, workspace_id, run_id) is RunStatus.REVIEW_READY
