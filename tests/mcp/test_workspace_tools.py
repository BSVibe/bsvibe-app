"""Workspace tool handler tests — Lift D3c."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

# Imported for table registration on the shared Base.metadata.
import backend.identity.workspaces_db  # noqa: F401
from backend.config import get_settings
from backend.identity.workspaces_db import WorkspaceRow
from backend.mcp.api import McpPrincipal, ToolContext, ToolError, ToolRegistry
from backend.mcp.tools import register_all_tools

from .._support import db_engine

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def db() -> AsyncIterator:
    get_settings.cache_clear()
    async with db_engine() as (engine, _is_pg):
        from sqlalchemy.ext.asyncio import async_sessionmaker

        yield async_sessionmaker(engine, expire_on_commit=False)
    get_settings.cache_clear()


@pytest.fixture
def workspace_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def user_id() -> uuid.UUID:
    return uuid.uuid4()


def _principal(*, workspace_id: uuid.UUID, user_id: uuid.UUID, scopes: tuple[str, ...]):
    return McpPrincipal(
        user_id=user_id,
        workspace_id=workspace_id,
        client_id="dcr-test",
        scopes=frozenset(scopes),
        jti=uuid.uuid4(),
    )


@pytest_asyncio.fixture
async def registry() -> ToolRegistry:
    reg = ToolRegistry()
    register_all_tools(reg)
    return reg


@pytest_asyncio.fixture
async def seeded(db, workspace_id) -> AsyncIterator[None]:
    """Stage-flush the workspace so FK references resolve under real Postgres."""
    async with db() as s:
        s.add(
            WorkspaceRow(
                id=workspace_id,
                name="acme",
                region="us-1",
                safe_mode=True,
            )
        )
        await s.commit()
    yield


async def test_get_returns_workspace_fields(db, workspace_id, user_id, registry, seeded) -> None:
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        out = await registry.call_tool("bsvibe_workspace_get", {}, ctx)
    assert out["id"] == str(workspace_id)
    assert out["name"] == "acme"
    assert out["region"] == "us-1"
    assert out["safe_mode"] is True
    assert out["audit_retention_days"] is None


async def test_get_requires_read_scope(db, workspace_id, user_id, registry, seeded) -> None:
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=()),
            session=s,
        )
        with pytest.raises(Exception, match="requires scope"):
            await registry.call_tool("bsvibe_workspace_get", {}, ctx)


async def test_rename_updates_name(db, workspace_id, user_id, registry, seeded) -> None:
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:admin",)),
            session=s,
        )
        out = await registry.call_tool("bsvibe_workspace_rename", {"name": "  renamed  "}, ctx)
    assert out["name"] == "renamed"  # trim is applied server-side
    assert out["id"] == str(workspace_id)

    # Re-read confirms persistence.
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        out2 = await registry.call_tool("bsvibe_workspace_get", {}, ctx)
    assert out2["name"] == "renamed"


async def test_rename_requires_admin_scope(db, workspace_id, user_id, registry, seeded) -> None:
    """mcp:write alone is NOT enough for workspace rename."""
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(
                workspace_id=workspace_id,
                user_id=user_id,
                scopes=("mcp:read", "mcp:write"),
            ),
            session=s,
        )
        with pytest.raises(Exception, match="requires scope"):
            await registry.call_tool("bsvibe_workspace_rename", {"name": "x"}, ctx)


async def test_rename_rejects_blank(db, workspace_id, user_id, registry, seeded) -> None:
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:admin",)),
            session=s,
        )
        # min_length=1 catches the empty string at validation time.
        with pytest.raises(ToolError, match="invalid arguments"):
            await registry.call_tool("bsvibe_workspace_rename", {"name": ""}, ctx)
        # ``"   "`` passes min_length but the handler trims and rejects.
        with pytest.raises(ToolError, match="must not be blank"):
            await registry.call_tool("bsvibe_workspace_rename", {"name": "   "}, ctx)


async def test_get_404_on_missing_row(db, workspace_id, user_id, registry) -> None:
    """No seed → workspace row doesn't exist → ToolError."""
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        with pytest.raises(ToolError, match="workspace not found"):
            await registry.call_tool("bsvibe_workspace_get", {}, ctx)
