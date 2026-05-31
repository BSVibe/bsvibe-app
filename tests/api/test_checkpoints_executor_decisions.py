"""/api/v1/checkpoints — executor B2b Decisions surface in the founder's needs-you.

B4 trust-integrity: when an executor run does NOT verify, it mints an honest
``Decision`` (``human_review_required`` / ``verification_failed``) instead of a
silent/hollow "shipped". Those Decisions are ``pending`` execution Decisions, so
the founder's needs-you / Decisions surface (``GET /api/v1/checkpoints``) MUST
list them — and with a meaningful question, not an empty string (the executor
records ``payload.reason`` rather than ``payload.question``).

SQLite by default; real Postgres when the env selects it. A Decision FKs to an
ExecutionRun, so the parent run is flushed before the child (mirrors
``tests/api/test_checkpoints_resolved.py``).
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
from backend.workflow.infrastructure.db import (
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


async def _seed_decision(db, *, ws, run_id, kind: str, payload: dict) -> uuid.UUID:
    """Seed a PENDING execution Decision exactly as the executor emits it —
    ``payload`` carries ``reason`` (NOT ``question``)."""
    decision_id = uuid.uuid4()
    async with db() as s:
        s.add(
            Decision(
                id=decision_id,
                run_id=run_id,
                workspace_id=ws,
                decision=kind,
                payload=payload,
                status=DecisionStatus.PENDING,
                created_at=_NOW - timedelta(hours=1),
            )
        )
        await s.commit()
    return decision_id


async def test_executor_decisions_surface_in_pending_checkpoints(client, db, workspace_id) -> None:
    """Both executor B2b Decision kinds appear in the founder's needs-you list."""
    run = await _seed_run(db, ws=workspace_id)
    hrr = await _seed_decision(
        db,
        ws=workspace_id,
        run_id=run,
        kind="human_review_required",
        payload={"reason": "no_verifiable_contract"},
    )
    vf = await _seed_decision(
        db,
        ws=workspace_id,
        run_id=run,
        kind="verification_failed",
        payload={"reason": "contract_failed"},
    )

    r = await client.get("/api/v1/checkpoints")
    assert r.status_code == 200, r.text
    rows = r.json()
    ids = {row["id"] for row in rows}
    assert str(hrr) in ids
    assert str(vf) in ids


async def test_executor_decision_carries_honest_question(client, db, workspace_id) -> None:
    """A reason-only executor Decision must NOT surface with an empty question —
    the founder needs an honest, human-readable line to act on."""
    run = await _seed_run(db, ws=workspace_id)
    await _seed_decision(
        db,
        ws=workspace_id,
        run_id=run,
        kind="verification_failed",
        payload={"reason": "contract_failed"},
    )

    r = await client.get("/api/v1/checkpoints")
    assert r.status_code == 200, r.text
    row = next(row for row in r.json() if row["decision"] == "verification_failed")
    assert row["question"].strip() != ""
