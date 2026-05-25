"""/api/v1/checkpoints/resolved — the founder's answered paused-run questions.

The Decisions "Resolved" tab folds resolved checkpoints in alongside the canon
decision log + resolved Safe-Mode deliveries. This endpoint is the
checkpoint-side source: every execution ``Decision`` that has been resolved
(status ``resolved``), newest-resolved first, scoped to the caller's workspace,
carrying the question + the founder's answer.

SQLite by default; real Postgres when the env selects it. A Decision FKs to an
ExecutionRun, so the parent run is flushed before the child (PG enforces the FK;
mirrors ``tests/api/test_run_detail.py``).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.api.deps import get_current_user, get_db_session, get_workspace_id
from backend.api.main import create_app
from backend.execution.db import (
    Decision,
    DecisionStatus,
    ExecutionBase,
    ExecutionRun,
    RunStatus,
)

from .._support import db_engine, fake_current_user

pytestmark = pytest.mark.asyncio

_NOW = datetime(2026, 5, 25, 12, 0, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def db():
    async with db_engine(ExecutionBase) as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def workspace_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest_asyncio.fixture
async def client(db, workspace_id: uuid.UUID):
    app = create_app()

    def _ws() -> uuid.UUID:
        return workspace_id

    async def _session():
        async with db() as s:
            yield s

    app.dependency_overrides[get_current_user] = fake_current_user()
    app.dependency_overrides[get_workspace_id] = _ws
    app.dependency_overrides[get_db_session] = _session

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _seed_run(db, *, ws: uuid.UUID) -> uuid.UUID:
    run_id = uuid.uuid4()
    async with db() as s:
        s.add(
            ExecutionRun(
                id=run_id,
                workspace_id=ws,
                status=RunStatus.RUNNING,
                payload={},
                created_at=_NOW - timedelta(hours=4),
            )
        )
        await s.commit()
    return run_id


async def _seed_decision(
    db,
    *,
    ws: uuid.UUID,
    run_id: uuid.UUID,
    status: DecisionStatus,
    question: str,
    resolution: str | None,
    resolved_at: datetime | None,
) -> uuid.UUID:
    decision_id = uuid.uuid4()
    async with db() as s:
        s.add(
            Decision(
                id=decision_id,
                run_id=run_id,
                workspace_id=ws,
                decision="needs_input",
                payload={"question": question},
                status=status,
                resolution=resolution,
                resolved_at=resolved_at,
                created_at=_NOW - timedelta(hours=3),
            )
        )
        await s.commit()
    return decision_id


async def test_resolved_lists_answered_newest_first(client, db, workspace_id) -> None:
    """Resolved decisions come back newest-resolved first with their answer; a
    still-pending decision is excluded (it belongs on the Pending tab)."""
    run = await _seed_run(db, ws=workspace_id)
    older = await _seed_decision(
        db,
        ws=workspace_id,
        run_id=run,
        status=DecisionStatus.RESOLVED,
        question="Deploy to prod?",
        resolution="yes, ship it",
        resolved_at=_NOW - timedelta(hours=2),
    )
    newer = await _seed_decision(
        db,
        ws=workspace_id,
        run_id=run,
        status=DecisionStatus.RESOLVED,
        question="Which region?",
        resolution="us-east",
        resolved_at=_NOW - timedelta(minutes=5),
    )
    await _seed_decision(
        db,
        ws=workspace_id,
        run_id=run,
        status=DecisionStatus.PENDING,
        question="Still waiting?",
        resolution=None,
        resolved_at=None,
    )

    r = await client.get("/api/v1/checkpoints/resolved")
    assert r.status_code == 200, r.text
    rows = r.json()
    assert [row["id"] for row in rows] == [str(newer), str(older)]
    assert rows[0]["question"] == "Which region?"
    assert rows[0]["resolution"] == "us-east"
    assert rows[0]["resolved_at"]
    assert rows[0]["run_id"] == str(run)


async def test_resolved_empty_when_only_pending(client, db, workspace_id) -> None:
    run = await _seed_run(db, ws=workspace_id)
    await _seed_decision(
        db,
        ws=workspace_id,
        run_id=run,
        status=DecisionStatus.PENDING,
        question="Pending one",
        resolution=None,
        resolved_at=None,
    )
    r = await client.get("/api/v1/checkpoints/resolved")
    assert r.status_code == 200, r.text
    assert r.json() == []


async def test_resolved_workspace_isolation(client, db, workspace_id) -> None:
    other = uuid.uuid4()
    run = await _seed_run(db, ws=other)
    await _seed_decision(
        db,
        ws=other,
        run_id=run,
        status=DecisionStatus.RESOLVED,
        question="secret",
        resolution="hidden",
        resolved_at=_NOW,
    )
    r = await client.get("/api/v1/checkpoints/resolved")
    assert r.status_code == 200, r.text
    assert r.json() == []
