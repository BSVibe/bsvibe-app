"""B11a — /api/v1/checkpoints structured-options listing + resolve validation.

The work LLM's ``ask_user_question`` may attach concrete ``options`` to a
paused-run Decision (Workflow §5 #4). When it does:

* GET  /api/v1/checkpoints      — each pending row surfaces the offered
  ``options`` so the Decisions UI can render them as a single-select.
* POST /api/v1/checkpoints/{id}/resolve — the founder's ``answer`` MUST be one
  of the offered options; off-list answers are rejected 400. Free-text mode
  (no options on the Decision) keeps the existing behaviour.

Mirrors :mod:`tests.api.test_checkpoints_executor_decisions` — SQLite by
default, real Postgres when the env selects it. A Decision FKs to an
ExecutionRun, so the parent run is flushed before the child.
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
from backend.execution.db import (
    Decision,
    DecisionStatus,
    ExecutionBase,
    ExecutionRun,
    RunStatus,
)

from .._support import db_engine, fake_current_user

pytestmark = pytest.mark.asyncio

_NOW = datetime(2026, 5, 26, 12, 0, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def db():
    async with db_engine(ExecutionBase) as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def workspace_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def founder_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest_asyncio.fixture
async def client(db, workspace_id: uuid.UUID, founder_id: uuid.UUID):
    app = create_app()

    def _ws() -> uuid.UUID:
        return workspace_id

    def _user_row() -> SimpleNamespace:
        return SimpleNamespace(id=founder_id)

    async def _session():
        async with db() as s:
            yield s

    app.dependency_overrides[get_current_user] = fake_current_user()
    app.dependency_overrides[get_workspace_id] = _ws
    app.dependency_overrides[get_current_user_row] = _user_row
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
                created_at=_NOW - timedelta(hours=2),
            )
        )
        await s.commit()
    return run_id


async def _seed_ask_user_question_decision(
    db, *, ws: uuid.UUID, run_id: uuid.UUID, payload: dict
) -> uuid.UUID:
    decision_id = uuid.uuid4()
    async with db() as s:
        s.add(
            Decision(
                id=decision_id,
                run_id=run_id,
                workspace_id=ws,
                decision="ask_user_question",
                payload=payload,
                status=DecisionStatus.PENDING,
                created_at=_NOW - timedelta(minutes=10),
            )
        )
        await s.commit()
    return decision_id


# ---------------------------------------------------------------------------
# Listing: options surface on the pending row
# ---------------------------------------------------------------------------


async def test_pending_checkpoint_surfaces_options(client, db, workspace_id) -> None:
    """B11a: a paused-run Decision whose payload carries ``options`` surfaces
    them on the listing response so the PWA can render a single-select."""
    run = await _seed_run(db, ws=workspace_id)
    cp = await _seed_ask_user_question_decision(
        db,
        ws=workspace_id,
        run_id=run,
        payload={
            "question": "Which database should I target?",
            "options": ["postgres", "sqlite", "mysql"],
        },
    )

    r = await client.get("/api/v1/checkpoints")
    assert r.status_code == 200, r.text
    row = next(row for row in r.json() if row["id"] == str(cp))
    assert row["options"] == ["postgres", "sqlite", "mysql"]


async def test_pending_checkpoint_without_options_returns_none(client, db, workspace_id) -> None:
    """B11a regression: free-text checkpoints (no options) return a falsy
    ``options`` field so the PWA falls back to the textarea control."""
    run = await _seed_run(db, ws=workspace_id)
    cp = await _seed_ask_user_question_decision(
        db,
        ws=workspace_id,
        run_id=run,
        payload={"question": "What should I do here?"},
    )

    r = await client.get("/api/v1/checkpoints")
    assert r.status_code == 200, r.text
    row = next(row for row in r.json() if row["id"] == str(cp))
    # ``None`` (or absent / empty list) — never a truthy options array.
    assert not row.get("options")


# ---------------------------------------------------------------------------
# Resolve validation
# ---------------------------------------------------------------------------


async def test_resolve_accepts_offered_option(client, db, workspace_id) -> None:
    """B11a: an answer that matches one of the offered options resolves cleanly."""
    run = await _seed_run(db, ws=workspace_id)
    cp = await _seed_ask_user_question_decision(
        db,
        ws=workspace_id,
        run_id=run,
        payload={
            "question": "Which database should I target?",
            "options": ["postgres", "sqlite", "mysql"],
        },
    )

    r = await client.post(
        f"/api/v1/checkpoints/{cp}/resolve",
        json={"answer": "postgres"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["resolution"] == "postgres"
    assert body["status"] == "resolved"


async def test_resolve_rejects_off_list_answer(client, db, workspace_id) -> None:
    """B11a: an answer NOT in the offered options is 400 — the founder picked
    something the agent never offered, so the resolve is refused (the Decision
    stays pending). Off-list means the LLM never asked about it; if the founder
    needs to say something else, they ought to be able to reach the agent
    differently. Strict for v1 — easy to relax later, hard to tighten."""
    run = await _seed_run(db, ws=workspace_id)
    cp = await _seed_ask_user_question_decision(
        db,
        ws=workspace_id,
        run_id=run,
        payload={
            "question": "Which database should I target?",
            "options": ["postgres", "sqlite", "mysql"],
        },
    )

    r = await client.post(
        f"/api/v1/checkpoints/{cp}/resolve",
        json={"answer": "duckdb"},
    )
    assert r.status_code == 400, r.text

    # The Decision is still pending; nothing was recorded.
    async with db() as s:
        d = await s.get(Decision, cp)
        assert d is not None
        assert d.status is DecisionStatus.PENDING
        assert d.resolution is None


async def test_resolve_free_text_when_no_options(client, db, workspace_id) -> None:
    """B11a regression: a Decision without ``options`` keeps the existing
    free-text behaviour — any non-empty answer resolves it."""
    run = await _seed_run(db, ws=workspace_id)
    cp = await _seed_ask_user_question_decision(
        db,
        ws=workspace_id,
        run_id=run,
        payload={"question": "What should I do here?"},
    )

    r = await client.post(
        f"/api/v1/checkpoints/{cp}/resolve",
        json={"answer": "ship it"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["resolution"] == "ship it"
