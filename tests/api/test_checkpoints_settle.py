"""B11b — Resolving a checkpoint emits a ``settle`` activity for knowledge reuse.

A resolved decision must become reusable knowledge: when the founder answers a
paused-run :class:`Decision`, the resolve endpoint records a
``settle``-class :class:`ExecutionRunActivity` carrying the question + answer +
stable run context. The :class:`SettleWorker` then drains it into the
workspace's BSage vault exactly like any verified-work observation — so a future
run with similar signals can surface the prior decision instead of re-asking.

These tests pin the WRITE contract at the API surface (the resolve endpoint adds
the row) and end-to-end through the settle worker (the row absorbs into a
``garden/seedling`` note carrying the question + answer text).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.api.deps import (
    get_current_user,
    get_current_user_row,
    get_db_session,
    get_workspace_id,
)
from backend.api.main import create_app
from backend.knowledge.infrastructure.workers.settle_worker import (
    KnowledgeSettleSink,
    SettleWorker,
    SettleWorkerConfig,
)
from backend.workflow.infrastructure.db import (
    Decision,
    DecisionStatus,
    ExecutionRun,
    ExecutionRunActivity,
    RunStatus,
)

from .._support import db_engine, fake_current_user

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


@pytest_asyncio.fixture
async def client(sf, workspace_id: uuid.UUID, founder_id: uuid.UUID):
    app = create_app()

    def _ws() -> uuid.UUID:
        return workspace_id

    def _user_row() -> SimpleNamespace:
        return SimpleNamespace(id=founder_id)

    async def _session():
        async with sf() as s:
            yield s

    app.dependency_overrides[get_current_user] = fake_current_user()
    app.dependency_overrides[get_workspace_id] = _ws
    app.dependency_overrides[get_current_user_row] = _user_row
    app.dependency_overrides[get_db_session] = _session

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _seed_pending(
    sf: async_sessionmaker[AsyncSession],
    workspace_id: uuid.UUID,
    *,
    question: str = "Which database should I target?",
    intent_text: str | None = "Build the answer file",
    options: list[str] | None = None,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Seed a paused run + pending Decision; return ``(run_id, decision_id)``."""
    async with sf() as s:
        payload: dict[str, object] = {"text": intent_text or ""}
        if intent_text:
            payload["intent_text"] = intent_text
        run = ExecutionRun(
            id=uuid.uuid4(),
            workspace_id=workspace_id,
            status=RunStatus.RUNNING,
            payload=payload,
            created_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC),
        )
        s.add(run)
        await s.flush()
        d_payload: dict[str, object] = {"question": question}
        if options is not None:
            d_payload["options"] = list(options)
        decision = Decision(
            id=uuid.uuid4(),
            run_id=run.id,
            workspace_id=workspace_id,
            decision="ask_user_question",
            payload=d_payload,
            status=DecisionStatus.PENDING,
        )
        s.add(decision)
        await s.commit()
        return run.id, decision.id


async def test_resolve_writes_decision_settle_activity(
    client: httpx.AsyncClient,
    sf: async_sessionmaker[AsyncSession],
    workspace_id: uuid.UUID,
    founder_id: uuid.UUID,
) -> None:
    """Resolving a checkpoint emits exactly one ``settle`` activity row carrying
    the decision-resolution payload (question, answer, kind, intent_text)."""
    _run_id, decision_id = await _seed_pending(
        sf, workspace_id, question="Which database should I target?"
    )

    r = await client.post(
        f"/api/v1/checkpoints/{decision_id}/resolve",
        json={"answer": "Use Postgres"},
    )
    assert r.status_code == 200, r.text

    async with sf() as s:
        activities = (
            (
                await s.execute(
                    select(ExecutionRunActivity).where(
                        ExecutionRunActivity.workspace_id == workspace_id,
                        ExecutionRunActivity.activity_type == "settle",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(activities) == 1, [a.payload for a in activities]
    payload = activities[0].payload
    assert payload["kind"] == "decision_resolution"
    assert payload["question"] == "Which database should I target?"
    assert payload["answer"] == "Use Postgres"
    assert payload["decision_id"] == str(decision_id)
    assert payload["resolved_by"] == str(founder_id)
    # ``intent_text`` is the stable run context — never LLM output.
    assert payload["intent_text"] == "Build the answer file"
    # ``verified`` is False — the resolution is an honest answer, NOT
    # verified-as-code (B4 trust integrity).
    assert payload["verified"] is False
    # A summary text the settle sink can use as the garden-note body / title.
    assert "Which database" in payload["summary"]
    assert "Use Postgres" in payload["summary"]


async def test_resolve_settle_activity_drains_into_vault(
    client: httpx.AsyncClient,
    sf: async_sessionmaker[AsyncSession],
    workspace_id: uuid.UUID,
    tmp_path: Path,
) -> None:
    """The decision-resolution settle activity drains into the workspace vault
    as a ``garden/seedling`` note carrying the question + answer text."""
    _run_id, decision_id = await _seed_pending(
        sf, workspace_id, question="Which framework?", intent_text="pick a web framework"
    )

    r = await client.post(
        f"/api/v1/checkpoints/{decision_id}/resolve",
        json={"answer": "FastAPI"},
    )
    assert r.status_code == 200, r.text

    worker = SettleWorker(
        session_factory=sf,
        sink=KnowledgeSettleSink(vault_root=tmp_path),
        config=SettleWorkerConfig(default_region="us-1"),
    )
    processed = await worker.drain_once()
    assert processed == 1

    ws_dir = tmp_path / "us-1" / str(workspace_id)
    notes = list(ws_dir.rglob("*.md"))
    # >= 1 because the promoter may emit a concept stub; but at minimum the
    # resolved decision must land as a garden note.
    garden_notes = [p for p in notes if "garden/" in p.as_posix()]
    assert garden_notes, f"no garden note written; vault contents: {notes}"
    bodies = [p.read_text(encoding="utf-8") for p in garden_notes]
    joined = "\n".join(bodies)
    assert "Which framework?" in joined
    assert "FastAPI" in joined


async def test_resolve_with_options_includes_options_in_settle_payload(
    client: httpx.AsyncClient,
    sf: async_sessionmaker[AsyncSession],
    workspace_id: uuid.UUID,
) -> None:
    """B11a structured options ride through onto the settle payload so a future
    run sees the option set the founder picked from."""
    _run_id, decision_id = await _seed_pending(
        sf,
        workspace_id,
        question="Which database?",
        options=["Postgres", "SQLite"],
    )

    r = await client.post(
        f"/api/v1/checkpoints/{decision_id}/resolve",
        json={"answer": "Postgres"},
    )
    assert r.status_code == 200, r.text

    async with sf() as s:
        row = (
            await s.execute(
                select(ExecutionRunActivity).where(
                    ExecutionRunActivity.activity_type == "settle",
                )
            )
        ).scalar_one()
    assert row.payload["options"] == ["Postgres", "SQLite"]


async def test_resolve_failure_writes_no_settle_activity(
    client: httpx.AsyncClient,
    sf: async_sessionmaker[AsyncSession],
    workspace_id: uuid.UUID,
) -> None:
    """An unknown checkpoint 404s and emits NO settle activity (no orphan write)."""
    r = await client.post(
        f"/api/v1/checkpoints/{uuid.uuid4()}/resolve",
        json={"answer": "x"},
    )
    assert r.status_code == 404

    async with sf() as s:
        rows = (await s.execute(select(ExecutionRunActivity))).scalars().all()
    assert rows == []
