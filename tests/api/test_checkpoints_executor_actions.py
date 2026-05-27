"""L-D2 — executor B2b Decision one-click actions (ship / discard).

Surfaces the action specs on the checkpoint list (so the PWA can render
localized buttons), validates ``action_key`` at resolve time, and
dispatches to the side-effecting handlers:

* ``ship``    — promotes WorkStep to verified/proved, mints a code
                Deliverable from the recorded artifact_refs, transitions
                run RUNNING → REVIEW_READY → SHIPPED.
* ``discard`` — transitions run RUNNING → CANCELLED (no deliverable).

SQLite by default; real Postgres when the env selects it. Mirrors the
shape of ``test_checkpoints_executor_decisions.py``.
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
    ProofState,
    RunStatus,
    WorkStep,
    WorkStepStatus,
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


async def _seed_run_with_step(db, *, ws: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:
    run_id = uuid.uuid4()
    step_id = uuid.uuid4()
    async with db() as s:
        s.add(
            ExecutionRun(
                id=run_id,
                workspace_id=ws,
                status=RunStatus.RUNNING,
                payload={},
                created_at=_NOW - timedelta(hours=2),
            )
        )
        s.add(
            WorkStep(
                id=step_id,
                run_id=run_id,
                workspace_id=ws,
                title="seed step",
                status=WorkStepStatus.RUNNING,
                proof_state=ProofState.UNTESTED,
                payload={},
                created_at=_NOW - timedelta(hours=2),
            )
        )
        await s.commit()
    return run_id, step_id


async def _seed_executor_decision(
    db, *, ws: uuid.UUID, run_id: uuid.UUID, kind: str, payload: dict
) -> uuid.UUID:
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
                created_at=_NOW - timedelta(minutes=10),
            )
        )
        await s.commit()
    return decision_id


# ---------------------------------------------------------------------------
# Surface: actions appear on GET /api/v1/checkpoints
# ---------------------------------------------------------------------------


async def test_executor_decision_lists_with_ship_and_discard_actions(
    client, db, workspace_id
) -> None:
    """Both executor B2b Decision kinds carry the ship + discard actions on
    the list response so the PWA renders one-click buttons."""
    run, _ = await _seed_run_with_step(db, ws=workspace_id)
    for kind in ("verification_failed", "human_review_required"):
        await _seed_executor_decision(
            db, ws=workspace_id, run_id=run, kind=kind, payload={"reason": "x"}
        )

    r = await client.get("/api/v1/checkpoints")
    assert r.status_code == 200, r.text
    for row in r.json():
        actions = row.get("actions")
        assert isinstance(actions, list) and len(actions) == 2
        keys = {a["key"] for a in actions}
        assert keys == {"ship", "discard"}
        # Both supported locales ship for client-side rendering.
        for a in actions:
            assert isinstance(a["label_en"], str) and a["label_en"]
            assert isinstance(a["label_ko"], str) and a["label_ko"]


async def test_ask_user_question_decision_carries_no_actions(client, db, workspace_id) -> None:
    """A plain ask_user_question Decision (no executor B2b kind) MUST NOT
    surface canned actions — those are reserved for the trust-integrity
    Decisions where ship/discard has well-defined semantics."""
    run, _ = await _seed_run_with_step(db, ws=workspace_id)
    await _seed_executor_decision(
        db,
        ws=workspace_id,
        run_id=run,
        kind="ask_user_question",
        payload={"question": "Which DB?", "options": ["postgres", "sqlite"]},
    )

    r = await client.get("/api/v1/checkpoints")
    assert r.status_code == 200, r.text
    row = next(row for row in r.json() if row["decision"] == "ask_user_question")
    assert row["actions"] is None


# ---------------------------------------------------------------------------
# Resolve: action_key validation
# ---------------------------------------------------------------------------


async def test_resolve_rejects_unknown_action_key(client, db, workspace_id) -> None:
    """An action_key not in the Decision kind's allowlist → 400."""
    run, _ = await _seed_run_with_step(db, ws=workspace_id)
    cp = await _seed_executor_decision(
        db,
        ws=workspace_id,
        run_id=run,
        kind="verification_failed",
        payload={"reason": "contract_failed", "artifact_refs": ["hello.py"]},
    )

    r = await client.post(
        f"/api/v1/checkpoints/{cp}/resolve",
        json={"action_key": "publish"},
    )
    assert r.status_code == 400, r.text


async def test_resolve_rejects_action_on_kind_with_no_actions(client, db, workspace_id) -> None:
    """An action_key on an ask_user_question Decision (no actions) → 400."""
    run, _ = await _seed_run_with_step(db, ws=workspace_id)
    cp = await _seed_executor_decision(
        db,
        ws=workspace_id,
        run_id=run,
        kind="ask_user_question",
        payload={"question": "Which DB?"},
    )

    r = await client.post(
        f"/api/v1/checkpoints/{cp}/resolve",
        json={"action_key": "ship"},
    )
    assert r.status_code == 400, r.text


# ---------------------------------------------------------------------------
# Resolve: ship side-effects
# ---------------------------------------------------------------------------


async def test_resolve_ship_promotes_workstep_creates_deliverable_and_ships_run(
    client, db, workspace_id
) -> None:
    """Founder ``ship`` → WorkStep verified/proved, code Deliverable minted
    from the Decision's recorded artifact_refs, run terminates at SHIPPED."""
    run_id, step_id = await _seed_run_with_step(db, ws=workspace_id)
    cp = await _seed_executor_decision(
        db,
        ws=workspace_id,
        run_id=run_id,
        kind="verification_failed",
        payload={
            "reason": "contract_failed",
            "artifact_refs": ["hello.py", "README.md"],
        },
    )

    r = await client.post(
        f"/api/v1/checkpoints/{cp}/resolve",
        json={"action_key": "ship"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "resolved"
    assert body["resolution"] == "ship"
    assert body["run_status"] == "shipped"

    async with db() as s:
        # WorkStep was promoted.
        step = await s.get(WorkStep, step_id)
        assert step is not None
        assert step.status is WorkStepStatus.VERIFIED
        assert step.proof_state is ProofState.PROVED
        # Run ended at SHIPPED.
        run = await s.get(ExecutionRun, run_id)
        assert run is not None
        assert run.status is RunStatus.SHIPPED
        # A code Deliverable was minted, carrying the override flag + refs.
        deliverable = (
            (await s.execute(select(Deliverable).where(Deliverable.run_id == run_id)))
            .scalars()
            .first()
        )
        assert deliverable is not None
        assert deliverable.deliverable_type is DeliverableType.CODE
        assert deliverable.payload["shipped_by_founder"] is True
        assert deliverable.payload["artifact_refs"] == ["hello.py", "README.md"]


# ---------------------------------------------------------------------------
# Resolve: discard side-effects
# ---------------------------------------------------------------------------


async def test_resolve_discard_abandons_run_with_no_deliverable(
    client, db, workspace_id
) -> None:
    """Founder ``discard`` → run goes to CANCELLED, no Deliverable minted."""
    run_id, _ = await _seed_run_with_step(db, ws=workspace_id)
    cp = await _seed_executor_decision(
        db,
        ws=workspace_id,
        run_id=run_id,
        kind="human_review_required",
        payload={"reason": "no_verifiable_contract"},
    )

    r = await client.post(
        f"/api/v1/checkpoints/{cp}/resolve",
        json={"action_key": "discard"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["resolution"] == "discard"
    assert r.json()["run_status"] == "cancelled"

    async with db() as s:
        run = await s.get(ExecutionRun, run_id)
        assert run is not None
        assert run.status is RunStatus.CANCELLED
        # No deliverable on discard.
        deliverable_count = len(
            (
                await s.execute(select(Deliverable).where(Deliverable.run_id == run_id))
            )
            .scalars()
            .all()
        )
        assert deliverable_count == 0


# ---------------------------------------------------------------------------
# Free-text path still works (regression)
# ---------------------------------------------------------------------------


async def test_resolve_freetext_still_resumes_run_to_open(client, db, workspace_id) -> None:
    """L-D2 regression: when no action_key is sent, the free-text path keeps
    the prior RUNNING → OPEN resume semantics (the loop re-picks)."""
    run_id, _ = await _seed_run_with_step(db, ws=workspace_id)
    cp = await _seed_executor_decision(
        db,
        ws=workspace_id,
        run_id=run_id,
        kind="ask_user_question",
        payload={"question": "Which DB?"},
    )

    r = await client.post(
        f"/api/v1/checkpoints/{cp}/resolve",
        json={"answer": "postgres"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["run_status"] == "open"
