"""checkpoint_resolution — the shared run-blocking-Decision resolve service.

C1 extracted the inline REST resolve logic into
:mod:`backend.workflow.application.checkpoint_resolution` so the MCP checkpoint
tools (C2) can reuse it. These tests pin the service-layer behaviour C2 will
rely on directly (not via HTTP) — resolving a pending ``ask_user_question``
Decision resolves it, folds the answer into the run payload, and resumes the
run RUNNING → OPEN — and assert the REST endpoint and the service produce
identical outcomes for the same input (the refactor is behaviour-preserving).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.api.deps import (
    get_current_user,
    get_current_user_row,
    get_db_session,
    get_workspace_id,
)
from backend.api.main import create_app
from backend.workflow.application.checkpoint_resolution import (
    CheckpointNotFound,
    CheckpointResolutionOutcome,
    InvalidAction,
    resolve_checkpoint,
)
from backend.workflow.infrastructure.db import (
    Decision,
    DecisionStatus,
    ExecutionRun,
    RunStatus,
)

from ..._support import db_engine, fake_current_user

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def sf():
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def workspace_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def founder_id() -> uuid.UUID:
    return uuid.uuid4()


async def _seed_pending_question(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    *,
    question: str = "Which DB?",
) -> tuple[uuid.UUID, uuid.UUID]:
    """Seed a RUNNING run + a PENDING ``ask_user_question`` Decision on it."""
    run = ExecutionRun(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        status=RunStatus.RUNNING,
        payload={"intent_text": "build the answer"},
        created_at=datetime.now(tz=UTC),
        updated_at=datetime.now(tz=UTC),
    )
    session.add(run)
    await session.flush()
    decision = Decision(
        id=uuid.uuid4(),
        run_id=run.id,
        workspace_id=workspace_id,
        decision="ask_user_question",
        payload={"question": question},
        status=DecisionStatus.PENDING,
    )
    session.add(decision)
    await session.flush()
    return run.id, decision.id


async def test_resolve_pending_question_resolves_folds_and_resumes(
    sf,
    workspace_id: uuid.UUID,
    founder_id: uuid.UUID,
) -> None:
    """The core C2 contract, proven at the service layer: resolving a pending
    ask_user_question Decision ⇒ RESOLVED + answer folded into
    ``run.payload['resolved_decisions']`` + run RUNNING → OPEN."""
    async with sf() as s:
        run_id, decision_id = await _seed_pending_question(s, workspace_id)
        await s.commit()

    async with sf() as s:
        outcome = await resolve_checkpoint(
            s,
            workspace_id=workspace_id,
            checkpoint_id=decision_id,
            answer="Use Postgres",
            actor_id=founder_id,
        )
        # The service owns no transaction — the caller commits.
        await s.commit()

    assert isinstance(outcome, CheckpointResolutionOutcome)
    assert outcome.decision_id == decision_id
    assert outcome.run_id == run_id
    assert outcome.status is DecisionStatus.RESOLVED
    assert outcome.resolution == "Use Postgres"
    assert outcome.run_status is RunStatus.OPEN

    async with sf() as s:
        decision = await s.get(Decision, decision_id)
        run = await s.get(ExecutionRun, run_id)
    assert decision is not None
    assert decision.status is DecisionStatus.RESOLVED
    assert decision.resolution == "Use Postgres"
    assert decision.resolved_by == founder_id
    assert run is not None
    assert run.status is RunStatus.OPEN
    resolved = run.payload["resolved_decisions"]
    assert resolved == [
        {
            "decision_id": str(decision_id),
            "question": "Which DB?",
            "answer": "Use Postgres",
        }
    ]


async def test_resolve_unknown_checkpoint_raises_not_found(
    sf,
    workspace_id: uuid.UUID,
    founder_id: uuid.UUID,
) -> None:
    async with sf() as s:
        with pytest.raises(CheckpointNotFound):
            await resolve_checkpoint(
                s,
                workspace_id=workspace_id,
                checkpoint_id=uuid.uuid4(),
                answer="x",
                actor_id=founder_id,
            )


async def test_resolve_empty_answer_no_action_raises_invalid(
    sf,
    workspace_id: uuid.UUID,
    founder_id: uuid.UUID,
) -> None:
    async with sf() as s:
        _run_id, decision_id = await _seed_pending_question(s, workspace_id)
        await s.commit()

    async with sf() as s:
        with pytest.raises(InvalidAction):
            await resolve_checkpoint(
                s,
                workspace_id=workspace_id,
                checkpoint_id=decision_id,
                answer="   ",
                actor_id=founder_id,
            )


async def test_rest_endpoint_and_service_produce_identical_outcomes(
    sf,
    workspace_id: uuid.UUID,
    founder_id: uuid.UUID,
) -> None:
    """The refactor is behaviour-preserving: resolving the *same* input through
    the REST endpoint and directly through the service yields the same Decision
    status / resolution / run status / folded payload."""
    # Two identical seeds (same question) so we can drive one via REST and one
    # via the service and compare the resulting state.
    async with sf() as s:
        rest_run_id, rest_decision_id = await _seed_pending_question(s, workspace_id)
        svc_run_id, svc_decision_id = await _seed_pending_question(s, workspace_id)
        await s.commit()

    # REST path.
    app = create_app()
    app.dependency_overrides[get_current_user] = fake_current_user()
    app.dependency_overrides[get_current_user_row] = lambda: SimpleNamespace(id=founder_id)
    app.dependency_overrides[get_workspace_id] = lambda: workspace_id

    async def _session():
        async with sf() as s:
            yield s

    app.dependency_overrides[get_db_session] = _session

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post(
            f"/api/v1/checkpoints/{rest_decision_id}/resolve",
            json={"answer": "Use Postgres"},
        )
    assert r.status_code == 200, r.text
    rest_body = r.json()

    # Service path.
    async with sf() as s:
        outcome = await resolve_checkpoint(
            s,
            workspace_id=workspace_id,
            checkpoint_id=svc_decision_id,
            answer="Use Postgres",
            actor_id=founder_id,
        )
        await s.commit()

    # Identical wire-relevant outcome.
    assert rest_body["status"] == outcome.status.value == DecisionStatus.RESOLVED.value
    assert rest_body["resolution"] == outcome.resolution == "Use Postgres"
    assert rest_body["run_status"] == outcome.run_status.value == RunStatus.OPEN.value

    # Identical persisted run payload fold (modulo the distinct ids).
    async with sf() as s:
        rest_run = await s.get(ExecutionRun, rest_run_id)
        svc_run = await s.get(ExecutionRun, svc_run_id)
    assert rest_run is not None
    assert svc_run is not None
    rest_fold = rest_run.payload["resolved_decisions"]
    svc_fold = svc_run.payload["resolved_decisions"]
    assert len(rest_fold) == len(svc_fold) == 1
    assert rest_fold[0]["question"] == svc_fold[0]["question"] == "Which DB?"
    assert rest_fold[0]["answer"] == svc_fold[0]["answer"] == "Use Postgres"
    assert rest_fold[0]["decision_id"] == str(rest_decision_id)
    assert svc_fold[0]["decision_id"] == str(svc_decision_id)
