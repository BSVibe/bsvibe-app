"""The monthly token budget is the user's own spend ceiling — unlimited by default.

Founder decision 2026-10-16: model tokens are BYO, so BSVibe has nothing to guard with a
platform-wide monthly cap. The budget stays as a ceiling an owner/admin may set or clear.
"""

from __future__ import annotations

import importlib.util
import uuid
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.api.deps import get_current_user, get_db_session
from backend.api.main import create_app
from backend.data.rls import workspace_session_scope
from backend.identity.db import MembershipRow, UserRow
from backend.identity.workspaces_db import DEFAULT_MONTHLY_TOKEN_BUDGET, WorkspaceRow
from backend.mcp.api import McpPrincipal, ToolContext, ToolError, ToolRegistry
from backend.mcp.tools import register_all_tools
from backend.workflow.application.intake_gate import IntakeVerdict, gate_webhook_work
from backend.workflow.application.workspace_token_budget import budget_window_start
from backend.workflow.infrastructure.db import ExecutionRun, RunStatus

from .._support import db_engine, fake_current_user
from .conftest import commit_per_workspace, flush_per_workspace

pytestmark = []


@pytest_asyncio.fixture
async def db():
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


async def _seed(db, role: str = "owner", *, budget: int | None = None, spent: int = 0):
    workspace_id, user_id = uuid.uuid4(), uuid.uuid4()
    sub = f"sub-{role}-{user_id}"
    inside = budget_window_start() + timedelta(seconds=1)
    async with db() as s:
        s.add(WorkspaceRow(id=workspace_id, name="ws", safe_mode=True, monthly_token_budget=budget))
        s.add(UserRow(id=user_id, supabase_user_id=sub, email=f"{user_id}@x"))
        await flush_per_workspace(s)
        s.add(MembershipRow(id=uuid.uuid4(), user_id=user_id, workspace_id=workspace_id, role=role))
        if spent:
            s.add(
                ExecutionRun(
                    id=uuid.uuid4(),
                    workspace_id=workspace_id,
                    status=RunStatus.SHIPPED,
                    payload={},
                    usage_prompt_tokens=spent,
                    usage_completion_tokens=0,
                    created_at=inside,
                    updated_at=inside,
                )
            )
        await commit_per_workspace(s)
    return workspace_id, user_id, sub


async def _budget(db, workspace_id: uuid.UUID) -> int | None:
    async with db() as s, workspace_session_scope(s, workspace_id):
        row = await s.get(WorkspaceRow, workspace_id)
        assert row is not None
        return row.monthly_token_budget


def _client(app, db) -> httpx.AsyncClient:
    async def _session():
        async with db() as s:
            yield s

    app.dependency_overrides[get_db_session] = _session
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


# --- default ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_default_workspace_has_no_budget(db) -> None:
    """Guard: if the default goes back to 50,000,000 this goes red."""
    workspace_id, _, _ = await _seed(db)
    assert DEFAULT_MONTHLY_TOKEN_BUDGET is None
    assert await _budget(db, workspace_id) is None


# --- webhook intake --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_webhook_is_admitted_without_a_budget(db) -> None:
    workspace_id, _, _ = await _seed(db, budget=None, spent=900_000_000)
    async with db() as s, workspace_session_scope(s, workspace_id):
        assert await gate_webhook_work(s, workspace_id) is IntakeVerdict.ADMIT


@pytest.mark.asyncio
async def test_webhook_is_refused_over_a_set_budget(db) -> None:
    workspace_id, _, _ = await _seed(db, budget=10_000, spent=12_000)
    async with db() as s, workspace_session_scope(s, workspace_id):
        assert await gate_webhook_work(s, workspace_id) is IntakeVerdict.REFUSE


# --- REST ------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["owner", "admin"])
async def test_rest_owner_or_admin_sets_and_clears_the_budget(db, role: str) -> None:
    workspace_id, _, sub = await _seed(db, role)
    app = create_app()
    app.dependency_overrides[get_current_user] = fake_current_user(sub)
    async with _client(app, db) as c:
        r = await c.put("/api/v1/workspace/token-budget", json={"monthly_token_budget": 5_000})
        assert r.status_code == 200, r.text
        assert r.json()["tokens"]["limit"] == 5_000
        assert await _budget(db, workspace_id) == 5_000

        r = await c.put("/api/v1/workspace/token-budget", json={"monthly_token_budget": None})
        assert r.status_code == 200, r.text
        assert r.json()["tokens"]["limit"] is None
        assert await _budget(db, workspace_id) is None


@pytest.mark.parametrize("role", ["editor", "viewer"])
@pytest.mark.asyncio
async def test_rest_other_roles_are_refused(db, role: str) -> None:
    workspace_id, _, sub = await _seed(db, role, budget=7_000)
    app = create_app()
    app.dependency_overrides[get_current_user] = fake_current_user(sub)
    async with _client(app, db) as c:
        r = await c.put("/api/v1/workspace/token-budget", json={"monthly_token_budget": 1})
    assert r.status_code == 403, r.text
    assert await _budget(db, workspace_id) == 7_000


@pytest.mark.parametrize("bad", [0, -5, "10", 1.5, True])
@pytest.mark.asyncio
async def test_rest_only_a_positive_integer_or_null_is_accepted(db, bad) -> None:
    workspace_id, _, sub = await _seed(db, "owner", budget=7_000)
    app = create_app()
    app.dependency_overrides[get_current_user] = fake_current_user(sub)
    async with _client(app, db) as c:
        r = await c.put("/api/v1/workspace/token-budget", json={"monthly_token_budget": bad})
    assert r.status_code == 422, r.text
    assert await _budget(db, workspace_id) == 7_000


# --- MCP -------------------------------------------------------------------------------


def _ctx(session, workspace_id, user_id) -> ToolContext:
    return ToolContext(
        principal=McpPrincipal(
            user_id=user_id,
            workspace_id=workspace_id,
            client_id="dcr-test",
            scopes=frozenset(("mcp:read", "mcp:admin")),
            jti=uuid.uuid4(),
        ),
        session=session,
    )


def _registry() -> ToolRegistry:
    reg = ToolRegistry()
    register_all_tools(reg)
    return reg


@pytest.mark.asyncio
async def test_mcp_owner_sets_and_clears_the_budget(db) -> None:
    workspace_id, user_id, _ = await _seed(db, "owner")
    reg = _registry()
    async with db() as s, workspace_session_scope(s, workspace_id):
        await reg.call_tool(
            "bsvibe_workspace_set_token_budget",
            {"monthly_token_budget": 9_000},
            _ctx(s, workspace_id, user_id),
        )
    assert await _budget(db, workspace_id) == 9_000
    async with db() as s, workspace_session_scope(s, workspace_id):
        await reg.call_tool(
            "bsvibe_workspace_set_token_budget",
            {"monthly_token_budget": None},
            _ctx(s, workspace_id, user_id),
        )
    assert await _budget(db, workspace_id) is None


@pytest.mark.asyncio
async def test_mcp_viewer_is_refused(db) -> None:
    workspace_id, user_id, _ = await _seed(db, "viewer", budget=7_000)
    async with db() as s, workspace_session_scope(s, workspace_id):
        with pytest.raises(ToolError, match="owner or admin"):
            await _registry().call_tool(
                "bsvibe_workspace_set_token_budget",
                {"monthly_token_budget": 1},
                _ctx(s, workspace_id, user_id),
            )
    assert await _budget(db, workspace_id) == 7_000


# --- migration -------------------------------------------------------------------------

_MIGRATION = (
    Path(__file__).parents[2]
    / "backend/data/migrations/versions/20261111_monthly_budget_unlimited.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("monthly_budget_unlimited", _MIGRATION)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _migration_engine() -> sa.Engine:
    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "CREATE TABLE workspaces (id INTEGER PRIMARY KEY, "
                "monthly_token_budget BIGINT DEFAULT 50000000)"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO workspaces (id, monthly_token_budget) "
                "VALUES (1, 50000000), (2, 7000000), (3, NULL), (4, 50000001)"
            )
        )
    return engine


def _budgets(engine: sa.Engine) -> dict[int, int | None]:
    with engine.connect() as conn:
        return dict(conn.execute(sa.text("SELECT id, monthly_token_budget FROM workspaces")).all())


def test_migration_nulls_only_the_old_default() -> None:
    mod, engine = _load_migration(), _migration_engine()
    with engine.begin() as conn, Operations.context(MigrationContext.configure(conn)):
        mod.upgrade()
    assert _budgets(engine) == {1: None, 2: 7_000_000, 3: None, 4: 50_000_001}


def test_migration_downgrade_restores_the_old_default_for_null() -> None:
    mod, engine = _load_migration(), _migration_engine()
    with engine.begin() as conn, Operations.context(MigrationContext.configure(conn)):
        mod.upgrade()
    with engine.begin() as conn, Operations.context(MigrationContext.configure(conn)):
        mod.downgrade()
    assert _budgets(engine) == {1: 50_000_000, 2: 7_000_000, 3: 50_000_000, 4: 50_000_001}
