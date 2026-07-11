"""/api/v1/run-routing — author run-routing rules (P1-L1b)."""

from __future__ import annotations

import uuid

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

import backend.router.routing.run_routing.db  # noqa: F401 — register run_routing_rules table
from backend.api.deps import get_current_user, get_db_session, get_workspace_id
from backend.api.main import create_app
from backend.identity.db import MembershipRow, UserRow  # noqa: F401 — register tables

from .._support import db_engine, fake_current_user

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def db():
    from backend.identity.workspaces_db import WorkspacesBase

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


async def test_compile_returns_proposals(client, monkeypatch) -> None:
    """POST /compile is a dry-run: it returns rule PROPOSALS (nothing persisted).
    The LLM + account gather live in compile_for_workspace, monkeypatched here."""
    import backend.api.v1.run_routing as rr

    async def _fake_compile(session, workspace_id, text, *, llm=None):
        assert text == "design → opus, rest → sonnet"
        return [
            {
                "name": "design → opus",
                "caller_id": "workflow.agent_loop.plan",
                "target": "opus",
                "priority": 10,
                "is_default": False,
            }
        ]

    monkeypatch.setattr(rr, "compile_for_workspace", _fake_compile)

    r = await client.post(
        "/api/v1/run-routing/compile", json={"text": "design → opus, rest → sonnet"}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["proposals"][0]["caller_id"] == "workflow.agent_loop.plan"
    assert body["proposals"][0]["target"] == "opus"

    # Dry-run — nothing was created.
    assert (await client.get("/api/v1/run-routing")).json() == []


async def test_compile_no_model_returns_400(client, monkeypatch) -> None:
    import backend.api.v1.run_routing as rr

    async def _no_model(session, workspace_id, text, *, llm=None):
        raise rr.NoCompileModelError

    monkeypatch.setattr(rr, "compile_for_workspace", _no_model)

    r = await client.post("/api/v1/run-routing/compile", json={"text": "route it"})
    assert r.status_code == 400
    assert "no model" in r.json()["detail"].lower()


async def test_list_callers_returns_known_callers(client) -> None:
    """The PWA rule form reads the selectable callers from here so the caller
    whitelist stays a single source of truth (the registry)."""
    r = await client.get("/api/v1/run-routing/callers")
    assert r.status_code == 200
    body = r.json()
    ids = {c["caller_id"] for c in body}
    assert "workflow.agent_loop.plan" in ids
    assert "chat.completions" in ids
    assert all(c["description"] for c in body)


async def test_create_list_delete_run_rule(client) -> None:
    # Empty initially.
    r = await client.get("/api/v1/run-routing")
    assert r.status_code == 200
    assert r.json() == []

    # Create a caller_id-keyed rule (Lift E2 — caller_id required for
    # non-default rules).
    body = {
        "name": "impl-stage",
        "caller_id": "workflow.agent_loop.act",
        "priority": 10,
        "target": "executor/opencode",
        "conditions": [{"field": "stage", "operator": "eq", "value": "impl"}],
    }
    r = await client.post("/api/v1/run-routing", json=body)
    assert r.status_code == 201, r.text
    created = r.json()
    assert created["name"] == "impl-stage"
    assert created["caller_id"] == "workflow.agent_loop.act"
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
        "caller_id": "workflow.frame",
        "priority": 0,
        "target": "t",
        "conditions": [{"field": "nonexistent", "operator": "eq", "value": "x"}],
    }
    r = await client.post("/api/v1/run-routing", json=body)
    assert r.status_code == 422


async def test_rejects_unknown_operator(client) -> None:
    body = {
        "name": "bad-op",
        "caller_id": "workflow.frame",
        "priority": 0,
        "target": "t",
        "conditions": [{"field": "stage", "operator": "matches", "value": "x"}],
    }
    r = await client.post("/api/v1/run-routing", json=body)
    assert r.status_code == 422


async def test_rejects_non_default_rule_without_caller_id(client) -> None:
    """Lift E2 — non-default rules MUST declare a caller_id."""
    body = {
        "name": "no-caller",
        "priority": 0,
        "target": "t",
        "conditions": [{"field": "stage", "operator": "eq", "value": "impl"}],
    }
    r = await client.post("/api/v1/run-routing", json=body)
    assert r.status_code == 422


async def test_rejects_unknown_caller_id(client) -> None:
    """Lift E2 — caller_id must match the registry or skill.<name>."""
    body = {
        "name": "bad-caller",
        "caller_id": "not.a.real.caller",
        "priority": 0,
        "target": "t",
        "conditions": [],
    }
    r = await client.post("/api/v1/run-routing", json=body)
    assert r.status_code == 422


async def test_accepts_skill_caller_id(client) -> None:
    """Lift E2 — skill.<name> caller_ids are permissively accepted at write time."""
    body = {
        "name": "skill-rule",
        "caller_id": "skill.my-custom-skill",
        "priority": 0,
        "target": "ollama/qwen",
        "conditions": [],
    }
    r = await client.post("/api/v1/run-routing", json=body)
    assert r.status_code == 201, r.text


async def test_delete_unknown_rule_404(client) -> None:
    r = await client.delete(f"/api/v1/run-routing/{uuid.uuid4()}")
    assert r.status_code == 404
