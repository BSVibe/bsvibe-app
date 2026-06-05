"""Skills tool handler tests — Lift D3c."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

# Imported for table registration on the shared Base.metadata.
import backend.identity.workspaces_db  # noqa: F401
from backend.config import get_settings
from backend.mcp.api import McpPrincipal, ToolContext, ToolError, ToolRegistry
from backend.mcp.tools import register_all_tools

from .._support import db_engine

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch) -> AsyncIterator:
    """Real fs sandbox under ``tmp_path/skills`` + reachable async session."""
    monkeypatch.setenv("BSVIBE_SKILLS_ROOT", str(tmp_path / "skills"))
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


async def test_list_empty_when_no_skills(db, workspace_id, user_id, registry) -> None:
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        out = await registry.call_tool("bsvibe_skills_list", {}, ctx)
    assert out == []


async def test_create_then_list_then_get_then_update(db, workspace_id, user_id, registry) -> None:
    # Create
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(
                workspace_id=workspace_id,
                user_id=user_id,
                scopes=("mcp:read", "mcp:write"),
            ),
            session=s,
        )
        created = await registry.call_tool(
            "bsvibe_skills_create",
            {
                "name": "Hello World",
                "summary": "Say hi calmly.",
                "system_prompt": "You greet warmly.",
            },
            ctx,
        )
    assert created["name"] == "hello-world"
    assert created["description"] == "Say hi calmly."
    assert created["has_system_prompt"] is True

    # List
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        listed = await registry.call_tool("bsvibe_skills_list", {}, ctx)
    assert isinstance(listed, list)
    assert any(m["name"] == "hello-world" for m in listed)

    # Get
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        got = await registry.call_tool("bsvibe_skills_get", {"name": "hello-world"}, ctx)
    assert got["description"] == "Say hi calmly."
    assert "You greet warmly." in got["system_prompt"]

    # Update
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(
                workspace_id=workspace_id,
                user_id=user_id,
                scopes=("mcp:read", "mcp:write"),
            ),
            session=s,
        )
        updated = await registry.call_tool(
            "bsvibe_skills_update",
            {
                "name": "hello-world",
                "summary": "Greet briskly.",
                "system_prompt": "Just say hi.",
            },
            ctx,
        )
    assert updated["description"] == "Greet briskly."


async def test_create_collision_raises(db, workspace_id, user_id, registry) -> None:
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(
                workspace_id=workspace_id,
                user_id=user_id,
                scopes=("mcp:read", "mcp:write"),
            ),
            session=s,
        )
        await registry.call_tool(
            "bsvibe_skills_create",
            {
                "name": "dup",
                "summary": "summary",
                "system_prompt": "prompt",
            },
            ctx,
        )
        with pytest.raises(ToolError, match="already exists"):
            await registry.call_tool(
                "bsvibe_skills_create",
                {
                    "name": "dup",
                    "summary": "summary",
                    "system_prompt": "prompt",
                },
                ctx,
            )


async def test_create_rejects_unsafe_name(db, workspace_id, user_id, registry) -> None:
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(
                workspace_id=workspace_id,
                user_id=user_id,
                scopes=("mcp:read", "mcp:write"),
            ),
            session=s,
        )
        with pytest.raises(ToolError, match="slug"):
            await registry.call_tool(
                "bsvibe_skills_create",
                {
                    "name": "../escape",
                    "summary": "summary",
                    "system_prompt": "prompt",
                },
                ctx,
            )


async def test_get_404_on_missing(db, workspace_id, user_id, registry) -> None:
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        with pytest.raises(ToolError, match="skill not found"):
            await registry.call_tool("bsvibe_skills_get", {"name": "ghost"}, ctx)


async def test_update_404_on_missing(db, workspace_id, user_id, registry) -> None:
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(
                workspace_id=workspace_id,
                user_id=user_id,
                scopes=("mcp:read", "mcp:write"),
            ),
            session=s,
        )
        with pytest.raises(ToolError, match="skill not found"):
            await registry.call_tool(
                "bsvibe_skills_update",
                {
                    "name": "ghost",
                    "summary": "x",
                    "system_prompt": "y",
                },
                ctx,
            )


async def test_create_requires_write_scope(db, workspace_id, user_id, registry) -> None:
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        with pytest.raises(Exception, match="requires scope"):
            await registry.call_tool(
                "bsvibe_skills_create",
                {
                    "name": "x",
                    "summary": "s",
                    "system_prompt": "p",
                },
                ctx,
            )


async def test_list_requires_read_scope(db, workspace_id, user_id, registry) -> None:
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=()),
            session=s,
        )
        with pytest.raises(Exception, match="requires scope"):
            await registry.call_tool("bsvibe_skills_list", {}, ctx)


async def test_skills_workspace_isolated(db, workspace_id, user_id, registry) -> None:
    """Skills created under workspace A must NOT appear in workspace B's list."""
    other_workspace = uuid.uuid4()
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(
                workspace_id=workspace_id,
                user_id=user_id,
                scopes=("mcp:read", "mcp:write"),
            ),
            session=s,
        )
        await registry.call_tool(
            "bsvibe_skills_create",
            {
                "name": "mine",
                "summary": "my skill",
                "system_prompt": "mine only",
            },
            ctx,
        )
    async with db() as s:
        other_ctx = ToolContext(
            principal=_principal(
                workspace_id=other_workspace, user_id=user_id, scopes=("mcp:read",)
            ),
            session=s,
        )
        listed = await registry.call_tool("bsvibe_skills_list", {}, other_ctx)
    assert listed == []
