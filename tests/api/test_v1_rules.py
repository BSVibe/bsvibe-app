"""/api/v1/rules — create / list / patch / delete routing rules.

These exercise the founder-facing CRUD over the EXISTING
:class:`~backend.gateway.rules.repository.RulesRepository`. The endpoints are
workspace + billing-account scoped exactly like the pre-existing list route
(``get_workspace_id`` + ``require_account_id``); a rule that belongs to another
workspace / account is invisible (404 on patch / delete, never returned by
list). The list response surfaces each rule's conditions so the UI can show
what a rule matches.

Like the other api tests we override ``get_current_user`` / ``get_workspace_id``
and inject the test session factory. The account axis is overridden with a
fixed id (mirrors ``test_v1_accounts_decisions``) so the orthogonal account
scoping is deterministic. No FK-child rows are inserted directly, so the
flush-parent-before-child gotcha doesn't bite here — but the repository's own
``create_rule`` flushes its parent before any condition child it adds.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.api.deps import (
    get_current_user,
    get_db_session,
    get_workspace_id,
    require_account_id,
)
from backend.api.main import create_app

# Importing the ORM rows registers routing_rules + rule_conditions on Base.
from backend.gateway.rules.db import RoutingRuleRow  # noqa: F401

from .._support import db_engine, fake_current_user

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def db():
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def workspace_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def account_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest_asyncio.fixture
async def client(db, workspace_id, account_id):
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
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_list_empty_initially(client) -> None:
    r = await client.get("/api/v1/rules")
    assert r.status_code == 200, r.text
    assert r.json() == []


async def test_create_persists_and_lists(client) -> None:
    body = {
        "name": "Substantial work",
        "target_model": "opencode/plan-builder",
        "priority": 10,
        "is_default": False,
        "is_active": True,
    }
    r = await client.post("/api/v1/rules", json=body)
    assert r.status_code == 201, r.text
    created = r.json()
    assert created["name"] == "Substantial work"
    assert created["target_model"] == "opencode/plan-builder"
    assert created["priority"] == 10
    assert created["is_default"] is False
    assert created["is_active"] is True
    assert uuid.UUID(created["id"])
    assert created["conditions"] == []

    listed = await client.get("/api/v1/rules")
    assert listed.status_code == 200
    rows = listed.json()
    assert len(rows) == 1
    assert rows[0]["id"] == created["id"]
    assert rows[0]["conditions"] == []


async def test_create_with_intent_condition_surfaces_in_list(client) -> None:
    body = {
        "name": "Simple chores",
        "target_model": "ollama/qwen3-coder:30b",
        "priority": 5,
        "is_default": False,
        "is_active": True,
        "conditions": [
            {
                "condition_type": "intent",
                "field": "classified_intent",
                "operator": "eq",
                "value": "chore",
            }
        ],
    }
    r = await client.post("/api/v1/rules", json=body)
    assert r.status_code == 201, r.text
    created = r.json()
    assert len(created["conditions"]) == 1
    cond = created["conditions"][0]
    assert cond["condition_type"] == "intent"
    assert cond["field"] == "classified_intent"
    assert cond["operator"] == "eq"
    assert cond["value"] == "chore"

    listed = await client.get("/api/v1/rules")
    rows = listed.json()
    assert len(rows[0]["conditions"]) == 1
    assert rows[0]["conditions"][0]["value"] == "chore"


async def test_create_default_catch_all_no_conditions(client) -> None:
    body = {
        "name": "Everything else",
        "target_model": "openai/gpt-4o-mini",
        "priority": 100,
        "is_default": True,
        "is_active": True,
    }
    r = await client.post("/api/v1/rules", json=body)
    assert r.status_code == 201, r.text
    assert r.json()["is_default"] is True
    assert r.json()["conditions"] == []


async def test_create_rejects_unknown_condition_field(client) -> None:
    """A condition field outside ALLOWED_FIELDS would never match — reject at
    the boundary rather than silently persist a dead rule."""
    body = {
        "name": "Bad rule",
        "target_model": "openai/gpt-4o-mini",
        "priority": 7,
        "conditions": [
            {
                "condition_type": "intent",
                "field": "__dunder__",
                "operator": "eq",
                "value": "x",
            }
        ],
    }
    r = await client.post("/api/v1/rules", json=body)
    assert r.status_code == 422, r.text


async def test_create_rejects_extra_field(client) -> None:
    body = {
        "name": "x",
        "target_model": "openai/gpt-4o-mini",
        "priority": 1,
        "surprise": "boom",
    }
    r = await client.post("/api/v1/rules", json=body)
    assert r.status_code == 422, r.text


async def test_patch_updates_rule(client) -> None:
    create = await client.post(
        "/api/v1/rules",
        json={"name": "r1", "target_model": "m1", "priority": 3},
    )
    rule_id = create.json()["id"]

    r = await client.patch(
        f"/api/v1/rules/{rule_id}",
        json={"name": "renamed", "target_model": "m2", "priority": 4, "is_default": True},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "renamed"
    assert body["target_model"] == "m2"
    assert body["priority"] == 4
    assert body["is_default"] is True


async def test_delete_removes_rule(client) -> None:
    create = await client.post(
        "/api/v1/rules",
        json={"name": "to-delete", "target_model": "m", "priority": 2},
    )
    rule_id = create.json()["id"]

    r = await client.delete(f"/api/v1/rules/{rule_id}")
    assert r.status_code == 204, r.text

    listed = await client.get("/api/v1/rules")
    assert listed.json() == []


async def test_delete_unknown_rule_404(client) -> None:
    r = await client.delete(f"/api/v1/rules/{uuid.uuid4()}")
    assert r.status_code == 404, r.text


async def test_patch_unknown_rule_404(client) -> None:
    r = await client.patch(
        f"/api/v1/rules/{uuid.uuid4()}",
        json={"name": "x", "target_model": "m", "priority": 1},
    )
    assert r.status_code == 404, r.text


async def test_delete_cross_account_is_404(db, workspace_id, account_id) -> None:
    """A rule created under account A is invisible (404) to account B in the
    same workspace — the orthogonal account axis isolates them."""
    other_account = uuid.uuid4()

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
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        # Create under account A.
        app.dependency_overrides[require_account_id] = lambda: account_id
        created = await c.post(
            "/api/v1/rules",
            json={"name": "a-only", "target_model": "m", "priority": 1},
        )
        assert created.status_code == 201, created.text
        rule_id = created.json()["id"]

        # Account B cannot delete it.
        app.dependency_overrides[require_account_id] = lambda: other_account
        r = await c.delete(f"/api/v1/rules/{rule_id}")
        assert r.status_code == 404, r.text

        # ...and cannot see it in its own list.
        listed = await c.get("/api/v1/rules")
        assert listed.json() == []


async def test_duplicate_name_within_account_is_409(client) -> None:
    body = {"name": "dup", "target_model": "m", "priority": 1}
    first = await client.post("/api/v1/rules", json=body)
    assert first.status_code == 201, first.text

    dup = await client.post(
        "/api/v1/rules",
        json={"name": "dup", "target_model": "m2", "priority": 2},
    )
    assert dup.status_code == 409, dup.text
