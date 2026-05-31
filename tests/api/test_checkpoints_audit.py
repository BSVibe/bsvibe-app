"""B15 — Resolving a checkpoint emits a ``DecisionResolved`` audit event.

Per Workflow §4 the founder's resolution of a paused-run Decision belongs on
the always-on audit stream. Pre-B15 the resolve endpoint wrote the Decision
row + a settle ExecutionRunActivity but never the audit event — the audit
stream was blind to the resolution. These tests pin the API contract.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
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
from backend.execution.audit_events import DecisionResolved
from backend.execution.db import (
    Decision,
    DecisionStatus,
    ExecutionRun,
    RunStatus,
)
from backend.extensions.implementations.audit.models import AuditOutboxRecord

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


async def _seed_pending(sf, workspace_id):
    async with sf() as s:
        run = ExecutionRun(
            id=uuid.uuid4(),
            workspace_id=workspace_id,
            status=RunStatus.RUNNING,
            payload={"intent_text": "build the answer"},
            created_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC),
        )
        s.add(run)
        await s.flush()
        decision = Decision(
            id=uuid.uuid4(),
            run_id=run.id,
            workspace_id=workspace_id,
            decision="ask_user_question",
            payload={"question": "Which DB?"},
            status=DecisionStatus.PENDING,
        )
        s.add(decision)
        await s.commit()
        return run.id, decision.id


async def test_resolve_emits_decision_resolved_audit_event(
    client: httpx.AsyncClient,
    sf,
    workspace_id: uuid.UUID,
    founder_id: uuid.UUID,
) -> None:
    _run_id, decision_id = await _seed_pending(sf, workspace_id)

    r = await client.post(
        f"/api/v1/checkpoints/{decision_id}/resolve",
        json={"answer": "Use Postgres"},
    )
    assert r.status_code == 200, r.text

    async with sf() as s:
        rows = (
            (
                await s.execute(
                    select(AuditOutboxRecord).where(
                        AuditOutboxRecord.event_type == DecisionResolved.DEFAULT_EVENT_TYPE
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1, [r.payload for r in rows]
    payload = rows[0].payload
    assert payload["data"]["decision_id"] == str(decision_id)
    assert payload["data"]["answer"] == "Use Postgres"
    assert payload["workspace_id"] == str(workspace_id)
    assert payload["actor"]["id"] == str(founder_id)


async def test_resolve_404_emits_no_audit_event(
    client: httpx.AsyncClient,
    sf,
    workspace_id: uuid.UUID,
) -> None:
    """A 404 (no such decision) emits NO orphan audit row."""
    r = await client.post(
        f"/api/v1/checkpoints/{uuid.uuid4()}/resolve",
        json={"answer": "x"},
    )
    assert r.status_code == 404

    async with sf() as s:
        rows = (await s.execute(select(AuditOutboxRecord))).scalars().all()
    assert rows == []
