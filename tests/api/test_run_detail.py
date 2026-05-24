"""/api/v1/runs/{id}/detail — run-detail read API (SQLite default, real PG on env).

The run-detail surface makes one externally-triggered ExecutionRun inspectable
(the Stitch "Triggered" screen): its trigger context (source / kind / intent /
product, pulled defensively out of the free-form ``payload``), its paused-run
Decision rows (the blocking questions + their resolution), the latest
VerificationResult outcome, and the resulting Deliverable id so the UI can link
out to its Delivery Report.

These tests seed an ``ExecutionRun`` plus its related rows and assert: the
trigger-context mapping, the decisions block, the verification outcome, the
deliverable id, workspace scoping (cross-workspace → 404), and that a run with a
sparse / empty payload degrades to a calm minimal detail (never a 500).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.api.deps import (
    get_current_user,
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
    ExecutionRunActivity,
    RunStatus,
    VerificationOutcome,
    VerificationResult,
)

from .._support import db_engine, fake_current_user

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def db():
    async with db_engine(ExecutionBase) as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def workspace_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest_asyncio.fixture
async def configured_client(db, workspace_id: uuid.UUID):
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
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def _seed_run(
    s,
    *,
    run_id: uuid.UUID,
    ws: uuid.UUID,
    status: RunStatus = RunStatus.RUNNING,
    payload: dict | None = None,
) -> None:
    s.add(
        ExecutionRun(
            id=run_id,
            workspace_id=ws,
            status=status,
            payload=payload if payload is not None else {},
            created_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC),
        )
    )
    await s.flush()


async def test_detail_returns_trigger_context(configured_client, db, workspace_id) -> None:
    """The free-form payload's trigger keys (source / kind / intent / product)
    surface defensively on the detail response."""
    run_id = uuid.uuid4()
    async with db() as s:
        await _seed_run(
            s,
            run_id=run_id,
            ws=workspace_id,
            status=RunStatus.RUNNING,
            payload={
                "source": "github",
                "trigger_kind": "webhook",
                "intent_text": "Mobile menu button is cut off on small screens",
                "product": "quantum-link",
                "extra_noise": {"ignored": True},
            },
        )
        await s.commit()

    r = await configured_client.get(f"/api/v1/runs/{run_id}/detail")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == str(run_id)
    assert body["status"] == "running"
    trigger = body["trigger"]
    assert trigger["source"] == "github"
    assert trigger["trigger_kind"] == "webhook"
    assert trigger["intent_text"] == "Mobile menu button is cut off on small screens"
    assert trigger["product"] == "quantum-link"
    # No decisions / verification / deliverable on a bare in-flight run.
    assert body["decisions"] == []
    assert body["verification"] is None
    assert body["deliverable_id"] is None


async def test_detail_includes_decisions_block(configured_client, db, workspace_id) -> None:
    """A paused run's Decision rows (decision / rationale / status / resolution)
    surface so the UI can show the blocking question and a resolve affordance."""
    run_id = uuid.uuid4()
    decision_id = uuid.uuid4()
    async with db() as s:
        await _seed_run(s, run_id=run_id, ws=workspace_id, status=RunStatus.RUNNING)
        s.add(
            Decision(
                id=decision_id,
                run_id=run_id,
                workspace_id=workspace_id,
                decision="ask_user_question",
                rationale="Because this came from outside, BSVibe is in Safe Mode.",
                payload={"question": "Let it continue?"},
                status=DecisionStatus.PENDING,
                created_at=datetime.now(tz=UTC),
            )
        )
        await s.commit()

    r = await configured_client.get(f"/api/v1/runs/{run_id}/detail")
    assert r.status_code == 200, r.text
    decisions = r.json()["decisions"]
    assert len(decisions) == 1
    d = decisions[0]
    assert d["id"] == str(decision_id)
    assert d["decision"] == "ask_user_question"
    assert d["question"] == "Let it continue?"
    assert d["rationale"] == "Because this came from outside, BSVibe is in Safe Mode."
    assert d["status"] == "pending"
    assert d["resolution"] is None


async def test_detail_includes_latest_verification(configured_client, db, workspace_id) -> None:
    """The latest VerificationResult outcome surfaces; older ones are not the
    one reported."""
    run_id = uuid.uuid4()
    base = datetime.now(tz=UTC)
    async with db() as s:
        await _seed_run(s, run_id=run_id, ws=workspace_id, status=RunStatus.REVIEW_READY)
        s.add(
            VerificationResult(
                id=uuid.uuid4(),
                run_id=run_id,
                workspace_id=workspace_id,
                outcome=VerificationOutcome.INCONCLUSIVE,
                contract={},
                result={},
                created_at=base,
            )
        )
        s.add(
            VerificationResult(
                id=uuid.uuid4(),
                run_id=run_id,
                workspace_id=workspace_id,
                outcome=VerificationOutcome.PASSED,
                contract={"checks": []},
                result={"summary": "19 passed"},
                created_at=base + timedelta(minutes=5),
            )
        )
        await s.commit()

    r = await configured_client.get(f"/api/v1/runs/{run_id}/detail")
    assert r.status_code == 200, r.text
    verification = r.json()["verification"]
    assert verification is not None
    assert verification["outcome"] == "passed"


async def test_detail_includes_deliverable_id(configured_client, db, workspace_id) -> None:
    """A shipped run's resulting Deliverable id surfaces so the UI can link to
    its Delivery Report."""
    run_id = uuid.uuid4()
    deliverable_id = uuid.uuid4()
    async with db() as s:
        await _seed_run(s, run_id=run_id, ws=workspace_id, status=RunStatus.SHIPPED)
        s.add(
            Deliverable(
                id=deliverable_id,
                run_id=run_id,
                workspace_id=workspace_id,
                deliverable_type=DeliverableType.PR,
                payload={"summary": "Fix the header"},
                created_at=datetime.now(tz=UTC),
            )
        )
        await s.commit()

    r = await configured_client.get(f"/api/v1/runs/{run_id}/detail")
    assert r.status_code == 200, r.text
    assert r.json()["deliverable_id"] == str(deliverable_id)


async def test_detail_cross_workspace_404(configured_client, db, workspace_id) -> None:
    """A run in another workspace is 404, never a leak."""
    other_ws = uuid.uuid4()
    theirs = uuid.uuid4()
    async with db() as s:
        await _seed_run(s, run_id=theirs, ws=other_ws, status=RunStatus.RUNNING)
        await s.commit()

    r = await configured_client.get(f"/api/v1/runs/{theirs}/detail")
    assert r.status_code == 404

    r2 = await configured_client.get(f"/api/v1/runs/{uuid.uuid4()}/detail")
    assert r2.status_code == 404


async def test_detail_sparse_payload_degrades_calmly(configured_client, db, workspace_id) -> None:
    """A run with an empty / sparse payload yields a calm minimal detail — all
    trigger fields null, no decisions / verification / deliverable — never a 500."""
    run_id = uuid.uuid4()
    async with db() as s:
        await _seed_run(s, run_id=run_id, ws=workspace_id, status=RunStatus.OPEN, payload={})
        await s.commit()

    r = await configured_client.get(f"/api/v1/runs/{run_id}/detail")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "open"
    trigger = body["trigger"]
    assert trigger["source"] is None
    assert trigger["trigger_kind"] is None
    assert trigger["intent_text"] is None
    assert trigger["product"] is None
    assert body["decisions"] == []
    assert body["verification"] is None
    assert body["deliverable_id"] is None


async def test_detail_tolerates_non_string_payload_values(
    configured_client, db, workspace_id
) -> None:
    """Odd payload value types (a number where a string is expected) degrade to
    None rather than 500ing the response model."""
    run_id = uuid.uuid4()
    async with db() as s:
        await _seed_run(
            s,
            run_id=run_id,
            ws=workspace_id,
            status=RunStatus.RUNNING,
            payload={"source": 123, "intent_text": ["not", "a", "string"], "text": "fallback"},
        )
        await s.commit()

    r = await configured_client.get(f"/api/v1/runs/{run_id}/detail")
    assert r.status_code == 200, r.text
    trigger = r.json()["trigger"]
    assert trigger["source"] is None
    # intent_text falls back to the `text` key when the canonical key is odd.
    assert trigger["intent_text"] == "fallback"


# ---------------------------------------------------------------------------
# Activity timeline — the run's STORY ("What I did")
# ---------------------------------------------------------------------------


async def _seed_activity(
    s,
    *,
    run_id: uuid.UUID,
    ws: uuid.UUID,
    activity_type: str,
    payload: dict | None = None,
    created_at: datetime,
) -> None:
    s.add(
        ExecutionRunActivity(
            id=uuid.uuid4(),
            run_id=run_id,
            workspace_id=ws,
            activity_type=activity_type,
            payload=payload if payload is not None else {},
            created_at=created_at,
        )
    )
    await s.flush()


async def test_detail_returns_activity_timeline_in_order(
    configured_client, db, workspace_id
) -> None:
    """Meaningful ExecutionRunActivity rows surface as a time-ordered timeline
    (oldest first) — each with its type + a short human label derived from the
    payload + the timestamp."""
    run_id = uuid.uuid4()
    base = datetime.now(tz=UTC)
    async with db() as s:
        await _seed_run(s, run_id=run_id, ws=workspace_id, status=RunStatus.REVIEW_READY)
        # A file-write tool_call → "Delivered calculator.py".
        await _seed_activity(
            s,
            run_id=run_id,
            ws=workspace_id,
            activity_type="tool_call",
            payload={"tool": "file_write", "ok": True, "writes": ["calculator.py"]},
            created_at=base + timedelta(minutes=1),
        )
        # A verify → "Verified".
        await _seed_activity(
            s,
            run_id=run_id,
            ws=workspace_id,
            activity_type="verify",
            payload={"outcome": "passed", "commands": 2},
            created_at=base + timedelta(minutes=2),
        )
        # A settle → "Settled into knowledge".
        await _seed_activity(
            s,
            run_id=run_id,
            ws=workspace_id,
            activity_type="settle",
            payload={"verified": True, "artifact_refs": ["calculator.py"], "summary": "done"},
            created_at=base + timedelta(minutes=3),
        )
        await s.commit()

    r = await configured_client.get(f"/api/v1/runs/{run_id}/detail")
    assert r.status_code == 200, r.text
    activities = r.json()["activities"]
    assert [a["type"] for a in activities] == ["tool_call", "verify", "settle"]
    # The file-write tool_call names the written path defensively.
    assert "calculator.py" in activities[0]["label"]
    # Verify / settle carry a human label.
    assert activities[1]["label"]
    assert activities[2]["label"]
    # Each entry carries its timestamp.
    assert all(a["created_at"] for a in activities)


async def test_detail_timeline_filters_noise(configured_client, db, workspace_id) -> None:
    """Noisy / low-signal activity rows (per-turn ``llm_turn`` chatter and
    non-write ``tool_call`` reads) are NOT surfaced on the founder timeline —
    only meaningful events are."""
    run_id = uuid.uuid4()
    base = datetime.now(tz=UTC)
    async with db() as s:
        await _seed_run(s, run_id=run_id, ws=workspace_id, status=RunStatus.RUNNING)
        await _seed_activity(
            s,
            run_id=run_id,
            ws=workspace_id,
            activity_type="llm_turn",
            payload={"content": "thinking...", "tool_calls": ["file_read"]},
            created_at=base + timedelta(seconds=10),
        )
        await _seed_activity(
            s,
            run_id=run_id,
            ws=workspace_id,
            activity_type="tool_call",
            payload={"tool": "file_read", "ok": True, "writes": []},
            created_at=base + timedelta(seconds=20),
        )
        await _seed_activity(
            s,
            run_id=run_id,
            ws=workspace_id,
            activity_type="verify",
            payload={"outcome": "passed", "commands": 1},
            created_at=base + timedelta(seconds=30),
        )
        await s.commit()

    r = await configured_client.get(f"/api/v1/runs/{run_id}/detail")
    assert r.status_code == 200, r.text
    activities = r.json()["activities"]
    # Only the meaningful "verify" survives; llm_turn + read-only tool_call drop.
    assert [a["type"] for a in activities] == ["verify"]


async def test_detail_timeline_synthesized_when_no_activities(
    configured_client, db, workspace_id
) -> None:
    """When no ExecutionRunActivity rows exist, the timeline is DERIVED from the
    deliverable + verification we already have (the DEFER fallback), rather than
    being empty — so the founder still sees a story."""
    run_id = uuid.uuid4()
    deliverable_id = uuid.uuid4()
    base = datetime.now(tz=UTC)
    async with db() as s:
        await _seed_run(s, run_id=run_id, ws=workspace_id, status=RunStatus.REVIEW_READY)
        s.add(
            VerificationResult(
                id=uuid.uuid4(),
                run_id=run_id,
                workspace_id=workspace_id,
                outcome=VerificationOutcome.PASSED,
                contract={},
                result={},
                created_at=base + timedelta(minutes=1),
            )
        )
        s.add(
            Deliverable(
                id=deliverable_id,
                run_id=run_id,
                workspace_id=workspace_id,
                deliverable_type=DeliverableType.CODE,
                payload={"summary": "Fix"},
                created_at=base + timedelta(minutes=2),
            )
        )
        await s.commit()

    r = await configured_client.get(f"/api/v1/runs/{run_id}/detail")
    assert r.status_code == 200, r.text
    body = r.json()
    types = [a["type"] for a in body["activities"]]
    # Synthesized from the rows we already carry.
    assert "verify" in types
    assert "deliver" in types
    assert body["timeline_source"] == "derived"


async def test_detail_timeline_source_recorded_when_activities_exist(
    configured_client, db, workspace_id
) -> None:
    """When real activity rows drive the timeline, ``timeline_source`` says so."""
    run_id = uuid.uuid4()
    async with db() as s:
        await _seed_run(s, run_id=run_id, ws=workspace_id, status=RunStatus.RUNNING)
        await _seed_activity(
            s,
            run_id=run_id,
            ws=workspace_id,
            activity_type="verify",
            payload={"outcome": "passed", "commands": 1},
            created_at=datetime.now(tz=UTC),
        )
        await s.commit()

    r = await configured_client.get(f"/api/v1/runs/{run_id}/detail")
    assert r.status_code == 200, r.text
    assert r.json()["timeline_source"] == "activities"


async def test_detail_timeline_empty_when_nothing_to_show(
    configured_client, db, workspace_id
) -> None:
    """A bare in-flight run with no activities / deliverable / verification has
    an empty timeline (and says it's derived) — never a 500."""
    run_id = uuid.uuid4()
    async with db() as s:
        await _seed_run(s, run_id=run_id, ws=workspace_id, status=RunStatus.OPEN, payload={})
        await s.commit()

    r = await configured_client.get(f"/api/v1/runs/{run_id}/detail")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["activities"] == []
    assert body["timeline_source"] == "derived"


async def test_detail_timeline_tolerates_odd_payload(configured_client, db, workspace_id) -> None:
    """An activity row with an odd / malformed payload (writes not a list, etc.)
    degrades to a calm generic label rather than 500ing the response model."""
    run_id = uuid.uuid4()
    async with db() as s:
        await _seed_run(s, run_id=run_id, ws=workspace_id, status=RunStatus.RUNNING)
        await _seed_activity(
            s,
            run_id=run_id,
            ws=workspace_id,
            activity_type="tool_call",
            payload={"tool": 123, "ok": "yes", "writes": "not-a-list"},
            created_at=datetime.now(tz=UTC),
        )
        await s.commit()

    r = await configured_client.get(f"/api/v1/runs/{run_id}/detail")
    assert r.status_code == 200, r.text
    # No write paths recoverable → the read-only tool_call drops out as noise.
    assert r.json()["activities"] == []
