"""L-P2 — ``ship_or_discard`` Decisions for REVIEW_READY runs.

When an agent loop verifies the work, AgentRunner transitions the run to
REVIEW_READY (and a code Deliverable is already attached). Previously the
run sat invisible in the founder UI — no Decision, no item on the
Decisions page. L-P2 synthesizes a ``ship_or_discard`` Decision on that
transition so the same one-click ship/discard buttons L-D2 ships for
executor B2b cases apply here too.

This file covers the SURFACING + ACTION side. The minting side (the
AgentRunner.transition synthesis itself) is exercised in
``tests/glue/test_agent_runner_worker.py``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.api.deps import (
    get_current_user,
    get_current_user_row,
    get_db_session,
    get_workspace_id,
)
from backend.api.main import create_app
from backend.execution.db import (
    Decision,
    DecisionStatus,
    Deliverable,
    DeliverableType,
    ExecutionBase,
    ExecutionRun,
    RunStatus,
)

from .._support import db_engine, fake_current_user

pytestmark = pytest.mark.asyncio

_NOW = datetime(2026, 5, 27, 12, 0, 0, tzinfo=UTC)


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


async def _seed_review_ready_run(db, *, ws: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:
    """Seed an ExecutionRun in REVIEW_READY with an existing code Deliverable —
    mirrors what the verifier's PASS path leaves on disk in production.

    PG enforces ``deliverables.run_id`` → ``execution_runs.id`` FK. Flush
    the parent run before adding the child deliverable so the FK can see
    the parent at INSERT time (real-PG won't auto-order siblings on
    commit; SQLite's default-off FK checks hid this locally)."""
    run_id = uuid.uuid4()
    deliverable_id = uuid.uuid4()
    async with db() as s:
        s.add(
            ExecutionRun(
                id=run_id,
                workspace_id=ws,
                status=RunStatus.REVIEW_READY,
                payload={},
                created_at=_NOW - timedelta(minutes=10),
            )
        )
        await s.flush()
        s.add(
            Deliverable(
                id=deliverable_id,
                run_id=run_id,
                workspace_id=ws,
                deliverable_type=DeliverableType.CODE,
                payload={"artifact_refs": ["hello.py"]},
            )
        )
        await s.commit()
    return run_id, deliverable_id


async def _seed_ship_or_discard_decision(db, *, ws: uuid.UUID, run_id: uuid.UUID) -> uuid.UUID:
    decision_id = uuid.uuid4()
    async with db() as s:
        s.add(
            Decision(
                id=decision_id,
                run_id=run_id,
                workspace_id=ws,
                decision="ship_or_discard",
                payload={"reason": "review_ready"},
                status=DecisionStatus.PENDING,
                created_at=_NOW - timedelta(minutes=5),
            )
        )
        await s.commit()
    return decision_id


# ---------------------------------------------------------------------------
# Surface: ship_or_discard carries ship + discard actions like executor B2b
# ---------------------------------------------------------------------------


async def test_ship_or_discard_decision_surfaces_with_ship_and_discard_actions(
    client, db, workspace_id
) -> None:
    run_id, _ = await _seed_review_ready_run(db, ws=workspace_id)
    cp = await _seed_ship_or_discard_decision(db, ws=workspace_id, run_id=run_id)

    r = await client.get("/api/v1/checkpoints")
    assert r.status_code == 200, r.text
    row = next(row for row in r.json() if row["id"] == str(cp))
    assert row["decision"] == "ship_or_discard"
    # Honest, kind-derived question even though payload has no ``question``.
    assert row["question"].strip()
    actions = row["actions"]
    assert isinstance(actions, list) and len(actions) == 2
    keys = {a["key"] for a in actions}
    assert keys == {"ship", "discard"}


# ---------------------------------------------------------------------------
# Resolve: ship is idempotent against the pre-existing Deliverable
# ---------------------------------------------------------------------------


async def test_resolve_ship_does_not_duplicate_existing_deliverable(
    client, db, workspace_id
) -> None:
    """The verifier's PASS path already minted a Deliverable when the run
    entered REVIEW_READY. The founder's ship action MUST NOT mint a
    second one — the first one is the verified artifact, and the
    deliverable surface (history / retraction / delivery) keys off id."""
    run_id, original_id = await _seed_review_ready_run(db, ws=workspace_id)
    cp = await _seed_ship_or_discard_decision(db, ws=workspace_id, run_id=run_id)

    r = await client.post(
        f"/api/v1/checkpoints/{cp}/resolve",
        json={"action_key": "ship"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["run_status"] == "shipped"

    async with db() as s:
        deliverables = (
            (await s.execute(select(Deliverable).where(Deliverable.run_id == run_id)))
            .scalars()
            .all()
        )
        # Exactly one — the verifier's original.
        assert len(deliverables) == 1
        assert deliverables[0].id == original_id


# ---------------------------------------------------------------------------
# Resolve: discard cancels the run + leaves the verifier's deliverable alone
# ---------------------------------------------------------------------------


async def test_resolve_discard_cancels_run(client, db, workspace_id) -> None:
    """Discard on a REVIEW_READY run transitions it to CANCELLED — the
    existing Deliverable row stays (retraction lives elsewhere); only the
    run terminal state changes."""
    run_id, _ = await _seed_review_ready_run(db, ws=workspace_id)
    cp = await _seed_ship_or_discard_decision(db, ws=workspace_id, run_id=run_id)

    r = await client.post(
        f"/api/v1/checkpoints/{cp}/resolve",
        json={"action_key": "discard"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["run_status"] == "cancelled"

    async with db() as s:
        run = await s.get(ExecutionRun, run_id)
        assert run is not None
        assert run.status is RunStatus.CANCELLED
