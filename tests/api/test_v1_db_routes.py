"""/api/v1/{rules,intents,runs} — end-to-end against real Postgres."""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.api.deps import (
    get_current_user,
    get_db_session,
    get_workspace_id,
    require_account_id,
)
from backend.api.main import create_app
from backend.execution.db import ExecutionBase, ExecutionRun, RunStatus
from backend.gateway.embedding.db import GatewayEmbeddingBase, IntentDefinitionRow
from backend.gateway.rules.db import GatewayRulesBase, RoutingRuleRow

from .._support import fake_current_user

PG_URL = os.environ.get(
    "BSVIBE_DATABASE_URL", "postgresql+asyncpg://bsvibe:bsvibe@localhost:5442/bsvibe"
)


pytestmark = pytest.mark.asyncio


async def _can_reach_pg() -> bool:
    try:
        engine = create_async_engine(PG_URL, future=True, pool_pre_ping=True)
        async with engine.connect() as conn:
            await conn.execute(__import__("sqlalchemy").text("SELECT 1"))
        await engine.dispose()
        return True
    except Exception:
        return False


@pytest_asyncio.fixture
async def db():
    use_pg = os.environ.get("BSVIBE_DATABASE_URL") and await _can_reach_pg()
    url = PG_URL if use_pg else "sqlite+aiosqlite:///:memory:"
    engine = create_async_engine(url, future=True)
    async with engine.begin() as conn:
        for base in (GatewayRulesBase, GatewayEmbeddingBase, ExecutionBase):
            await conn.run_sync(base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    yield sm
    if use_pg:
        async with engine.begin() as conn:
            for base in (ExecutionBase, GatewayEmbeddingBase, GatewayRulesBase):
                await conn.run_sync(base.metadata.drop_all)
    await engine.dispose()


@pytest.fixture
def workspace_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def account_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest_asyncio.fixture
async def configured_client(db, workspace_id: uuid.UUID, account_id: uuid.UUID):
    app = create_app()

    def _ws() -> uuid.UUID:
        return workspace_id

    def _acct() -> uuid.UUID:
        return account_id

    async def _session():
        async with db() as s:
            yield s

    app.dependency_overrides[get_current_user] = fake_current_user()
    app.dependency_overrides[get_workspace_id] = _ws
    app.dependency_overrides[require_account_id] = _acct
    app.dependency_overrides[get_db_session] = _session

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def test_rules_list(configured_client, db, workspace_id, account_id) -> None:
    async with db() as s:
        s.add(
            RoutingRuleRow(
                id=uuid.uuid4(),
                workspace_id=workspace_id,
                account_id=account_id,
                name="cheap",
                priority=1,
                is_active=True,
                is_default=True,
                target_model="anthropic/claude-haiku-4-5",
            )
        )
        await s.commit()
    r = await configured_client.get("/api/v1/rules")
    assert r.status_code == 200, r.text
    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["name"] == "cheap"
    assert rows[0]["target_model"] == "anthropic/claude-haiku-4-5"


async def test_intents_list(configured_client, db, workspace_id, account_id) -> None:
    async with db() as s:
        s.add(
            IntentDefinitionRow(
                id=uuid.uuid4(),
                workspace_id=workspace_id,
                account_id=account_id,
                name="summarize",
            )
        )
        await s.commit()
    r = await configured_client.get("/api/v1/intents")
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body) == 1
    assert body[0]["name"] == "summarize"


async def test_runs_list_and_get(configured_client, db, workspace_id) -> None:
    run_id = uuid.uuid4()
    other_ws_run_id = uuid.uuid4()
    async with db() as s:
        s.add(
            ExecutionRun(
                id=run_id,
                workspace_id=workspace_id,
                status=RunStatus.OPEN,
                payload={},
                created_at=datetime.now(tz=UTC),
                updated_at=datetime.now(tz=UTC),
            )
        )
        # Another workspace's run — MUST NOT appear in list / GET.
        s.add(
            ExecutionRun(
                id=other_ws_run_id,
                workspace_id=uuid.uuid4(),
                status=RunStatus.SHIPPED,
                payload={},
                created_at=datetime.now(tz=UTC),
                updated_at=datetime.now(tz=UTC),
            )
        )
        await s.commit()

    r = await configured_client.get("/api/v1/runs")
    assert r.status_code == 200, r.text
    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["id"] == str(run_id)
    assert rows[0]["status"] == "open"

    r2 = await configured_client.get(f"/api/v1/runs/{run_id}")
    assert r2.status_code == 200

    r3 = await configured_client.get(f"/api/v1/runs/{other_ws_run_id}")
    assert r3.status_code == 404


async def test_runs_list_empty(configured_client) -> None:
    r = await configured_client.get("/api/v1/runs")
    assert r.status_code == 200
    assert r.json() == []
