"""PR7 — the ``merge_conflict_review`` Decision on the founder's checkpoint surface.

An AMBIGUOUS merge conflict the agent refused to guess surfaces as a pending
Decision that:

* lists with a non-empty question + the retry/discard one-click actions (NO ship),
* resolves ``retry`` → run resumes RUNNING → OPEN (the agent re-resolves with the
  founder's guidance; the merge-watch loop re-freshens the re-pushed head),
* resolves ``discard`` → run goes to CANCELLED (the merge-watch worker then closes
  the orphaned PR on its next poll).

SQLite by default; real Postgres when the env selects it. Mirrors
``test_checkpoints_executor_actions.py``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.api.deps import (
    get_current_user,
    get_current_user_row,
    get_db_session,
    get_workspace_id,
)
from backend.api.main import create_app
from backend.workflow.infrastructure.db import (
    Decision,
    DecisionStatus,
    ExecutionBase,
    ExecutionRun,
    ProofState,
    RunStatus,
    WorkStep,
    WorkStepStatus,
)

from .._support import db_engine, fake_current_user

pytestmark = pytest.mark.asyncio

_NOW = datetime(2026, 7, 29, 12, 0, 0, tzinfo=UTC)


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
    founder_id = uuid.uuid4()

    def _ws() -> uuid.UUID:
        return workspace_id

    def _user_row() -> SimpleNamespace:
        return SimpleNamespace(id=founder_id)

    async def _session():
        async with db() as s:
            yield s

    app.dependency_overrides[get_current_user] = fake_current_user()
    app.dependency_overrides[get_current_user_row] = _user_row
    app.dependency_overrides[get_workspace_id] = _ws
    app.dependency_overrides[get_db_session] = _session

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _seed_run_with_step(db, *, ws: uuid.UUID) -> uuid.UUID:
    run_id = uuid.uuid4()
    async with db() as s:
        s.add(
            ExecutionRun(
                id=run_id,
                workspace_id=ws,
                status=RunStatus.RUNNING,
                payload={"merge_conflict_resolving": True},
                created_at=_NOW - timedelta(hours=1),
            )
        )
        s.add(
            WorkStep(
                id=uuid.uuid4(),
                run_id=run_id,
                workspace_id=ws,
                title="seed step",
                status=WorkStepStatus.RUNNING,
                proof_state=ProofState.UNTESTED,
                payload={},
                created_at=_NOW - timedelta(hours=1),
            )
        )
        await s.commit()
    return run_id


async def _seed_decision(db, *, ws: uuid.UUID, run_id: uuid.UUID, payload: dict) -> uuid.UUID:
    decision_id = uuid.uuid4()
    async with db() as s:
        s.add(
            Decision(
                id=decision_id,
                run_id=run_id,
                workspace_id=ws,
                decision="merge_conflict_review",
                payload=payload,
                status=DecisionStatus.PENDING,
                created_at=_NOW - timedelta(minutes=5),
            )
        )
        await s.commit()
    return decision_id


async def test_lists_with_question_and_retry_discard_actions(client, db, workspace_id) -> None:
    run = await _seed_run_with_step(db, ws=workspace_id)
    await _seed_decision(
        db,
        ws=workspace_id,
        run_id=run,
        payload={"question": "main replaced foo(); branch renamed it — which wins?"},
    )

    r = await client.get("/api/v1/checkpoints")
    assert r.status_code == 200, r.text
    row = next(x for x in r.json() if x["decision"] == "merge_conflict_review")
    assert row["question"]  # non-empty
    actions = row["actions"]
    assert isinstance(actions, list)
    assert {a["key"] for a in actions} == {"retry", "discard"}
    for a in actions:
        assert a["label_en"] and a["label_ko"]


async def test_retry_resumes_run_to_open(client, db, workspace_id) -> None:
    run_id = await _seed_run_with_step(db, ws=workspace_id)
    cp = await _seed_decision(db, ws=workspace_id, run_id=run_id, payload={"reason": "ambiguous"})

    r = await client.post(f"/api/v1/checkpoints/{cp}/resolve", json={"action_key": "retry"})
    assert r.status_code == 200, r.text
    assert r.json()["run_status"] == "open"

    async with db() as s:
        run = await s.get(ExecutionRun, run_id)
        assert run is not None and run.status is RunStatus.OPEN


async def test_discard_cancels_run(client, db, workspace_id) -> None:
    run_id = await _seed_run_with_step(db, ws=workspace_id)
    cp = await _seed_decision(db, ws=workspace_id, run_id=run_id, payload={"reason": "ambiguous"})

    r = await client.post(f"/api/v1/checkpoints/{cp}/resolve", json={"action_key": "discard"})
    assert r.status_code == 200, r.text
    assert r.json()["run_status"] == "cancelled"

    async with db() as s:
        run = await s.get(ExecutionRun, run_id)
        assert run is not None and run.status is RunStatus.CANCELLED
