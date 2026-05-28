"""/api/v1/run-routing — author run-routing rules (P1-L1b)."""

from __future__ import annotations

import uuid

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

import backend.routing.db  # noqa: F401 — register run_routing_rules table
from backend.api.deps import get_current_user, get_db_session, get_workspace_id
from backend.api.main import create_app
from backend.identity.db import MembershipRow, UserRow  # noqa: F401 — register tables

from .._support import db_engine, fake_current_user

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def db():
    from backend.workspaces.db import WorkspacesBase

    async with db_engine(WorkspacesBase) as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


@pytest_asyncio.fixture
async def client(db):
    app = create_app()
    workspace_id = uuid.uuid4()

    async def _session():
        async with db() as s:
            yield s

    app.dependency_overrides[get_current_user] = fake_current_user()
    app.dependency_overrides[get_workspace_id] = lambda: workspace_id
    app.dependency_overrides[get_db_session] = _session

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_create_list_delete_run_rule(client) -> None:
    # Empty initially.
    r = await client.get("/api/v1/run-routing")
    assert r.status_code == 200
    assert r.json() == []

    # Create a stage→executor rule.
    body = {
        "name": "impl-stage",
        "priority": 10,
        "target": "executor/opencode",
        "conditions": [{"field": "stage", "operator": "eq", "value": "impl"}],
    }
    r = await client.post("/api/v1/run-routing", json=body)
    assert r.status_code == 201, r.text
    created = r.json()
    assert created["name"] == "impl-stage"
    assert created["target"] == "executor/opencode"
    assert created["conditions"] == [
        {"field": "stage", "operator": "eq", "value": "impl", "negate": False}
    ]
    rule_id = created["id"]

    # List shows it.
    r = await client.get("/api/v1/run-routing")
    assert len(r.json()) == 1

    # Delete.
    r = await client.delete(f"/api/v1/run-routing/{rule_id}")
    assert r.status_code == 204
    r = await client.get("/api/v1/run-routing")
    assert r.json() == []


async def test_duplicate_name_conflicts(client) -> None:
    body = {"name": "dup", "priority": 0, "target": "ollama/x", "is_default": True}
    assert (await client.post("/api/v1/run-routing", json=body)).status_code == 201
    r = await client.post("/api/v1/run-routing", json=body)
    assert r.status_code == 409


async def test_rejects_unknown_condition_field(client) -> None:
    body = {
        "name": "bad-field",
        "priority": 0,
        "target": "t",
        "conditions": [{"field": "nonexistent", "operator": "eq", "value": "x"}],
    }
    r = await client.post("/api/v1/run-routing", json=body)
    assert r.status_code == 422


async def test_rejects_unknown_operator(client) -> None:
    body = {
        "name": "bad-op",
        "priority": 0,
        "target": "t",
        "conditions": [{"field": "stage", "operator": "matches", "value": "x"}],
    }
    r = await client.post("/api/v1/run-routing", json=body)
    assert r.status_code == 422


async def test_delete_unknown_rule_404(client) -> None:
    r = await client.delete(f"/api/v1/run-routing/{uuid.uuid4()}")
    assert r.status_code == 404
