"""The run's STORY speaks the workspace's language.

The timeline HEADING was localized ("진행한 일") while every event LINE under it
came from the backend as English — "Delivered x.py", "Settled into knowledge".
A KO founder read a Korean heading over an English list.

Localizing at the PRODUCER is the house pattern (``run_persistence`` and
``agent_runner`` both do ``load_workspace_language`` then build the sentence),
and it is the prescription from the catalog-leak defect: the producer knows the
language, the renderer must not have to guess.

These assert the ENDPOINT payload, not ``_activity_label`` — the label helper
returning Korean proves nothing if the endpoint never passes the language.
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

# Imported for table registration on the unified Base.metadata.
from backend.identity.workspaces_db import WorkspaceRow
from backend.workflow.infrastructure.db import (
    ExecutionRun,
    ExecutionRunActivity,
    RunStatus,
)

from .._support import db_engine, fake_current_user

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def db():
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def workspace_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest_asyncio.fixture
async def configured_client(db, workspace_id: uuid.UUID):
    app = create_app()

    async def _session():
        async with db() as s:
            yield s

    app.dependency_overrides[get_current_user] = fake_current_user()
    app.dependency_overrides[get_workspace_id] = lambda: workspace_id
    app.dependency_overrides[get_db_session] = _session
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def _seed(db, ws: uuid.UUID, language: str) -> uuid.UUID:
    """A workspace in ``language`` + a run carrying one of every timeline event."""
    run_id = uuid.uuid4()
    base = datetime.now(tz=UTC)
    async with db() as s:
        s.add(WorkspaceRow(id=ws, name="w", language=language))
        s.add(ExecutionRun(id=run_id, workspace_id=ws, status=RunStatus.REVIEW_READY, payload={}))
        await s.flush()
        events = [
            ("tool_call", {"tool": "file_write", "ok": True, "writes": ["calculator.py"]}),
            ("verify", {"outcome": "passed"}),
            ("settle", {"verified": True}),
            (
                "routing_decision",
                {
                    "caller_id": "workflow.agent_loop.act",
                    "source": "explicit_rule",
                    "target": "opus",
                },
            ),
            ("error", {}),
        ]
        for i, (kind, payload) in enumerate(events):
            s.add(
                ExecutionRunActivity(
                    id=uuid.uuid4(),
                    run_id=run_id,
                    workspace_id=ws,
                    activity_type=kind,
                    payload=payload,
                    created_at=base + timedelta(minutes=i),
                )
            )
        await s.commit()
    return run_id


async def test_a_korean_workspace_reads_a_korean_story(configured_client, db, workspace_id) -> None:
    """Every surfaced line is Korean — not just the ones easy to translate."""
    run_id = await _seed(db, workspace_id, "ko")

    r = await configured_client.get(f"/api/v1/runs/{run_id}/detail")
    assert r.status_code == 200, r.text
    labels = {a["type"]: a["label"] for a in r.json()["activities"]}

    assert "calculator.py" in labels["tool_call"]
    for kind, label in labels.items():
        assert not any(
            c.isascii() and c.isalpha()
            for c in label.replace("calculator.py", "")
            .replace("workflow.agent_loop.act", "")
            .replace("opus", "")
        ), f"{kind} still leaks English: {label!r}"


async def test_an_english_workspace_is_unchanged(configured_client, db, workspace_id) -> None:
    """Regression guard — EN keeps the exact wording it has today."""
    run_id = await _seed(db, workspace_id, "en")

    r = await configured_client.get(f"/api/v1/runs/{run_id}/detail")
    assert r.status_code == 200, r.text
    labels = {a["type"]: a["label"] for a in r.json()["activities"]}

    assert labels["tool_call"] == "Delivered calculator.py"
    assert labels["verify"] == "Verified the work"
    assert labels["settle"] == "Settled into knowledge"
    assert labels["error"] == "Hit a problem"
    assert labels["routing_decision"] == (
        "Routed workflow.agent_loop.act to opus (your routing rule)"
    )


async def test_a_workspace_row_that_does_not_exist_falls_back_to_english(
    configured_client, db, workspace_id
) -> None:
    """No workspace row (the shape most existing tests use) must not 500 —
    ``load_workspace_language`` defaults to ``en``."""
    run_id = uuid.uuid4()
    async with db() as s:
        s.add(
            ExecutionRun(id=run_id, workspace_id=workspace_id, status=RunStatus.RUNNING, payload={})
        )
        await s.flush()
        s.add(
            ExecutionRunActivity(
                id=uuid.uuid4(),
                run_id=run_id,
                workspace_id=workspace_id,
                activity_type="settle",
                payload={},
                created_at=datetime.now(tz=UTC),
            )
        )
        await s.commit()

    r = await configured_client.get(f"/api/v1/runs/{run_id}/detail")
    assert r.status_code == 200, r.text
    assert r.json()["activities"][0]["label"] == "Settled into knowledge"
