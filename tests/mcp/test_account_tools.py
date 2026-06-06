"""Account tool handler tests — Lift D3d."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

# Imported for table registration on the shared Base.metadata.
import backend.identity.db  # noqa: F401
import backend.identity.workspaces_db  # noqa: F401
import backend.router.accounts.account_models  # noqa: F401
from backend.config import get_settings
from backend.identity.db import MembershipRow, UserRow
from backend.identity.workspaces_db import WorkspaceRow
from backend.mcp.api import McpPrincipal, ToolContext, ToolError, ToolRegistry, ToolScopeDenied
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
async def seeded(db, workspace_id, user_id) -> AsyncIterator[None]:
    """Stage-flush workspace + user + membership parents so PG FKs resolve."""
    async with db() as s:
        s.add(WorkspaceRow(id=workspace_id, name="ws", region="us-1"))
        s.add(UserRow(id=user_id, supabase_user_id=f"supabase|{user_id}"))
        await s.flush()
        s.add(
            MembershipRow(
                id=uuid.uuid4(),
                user_id=user_id,
                workspace_id=workspace_id,
                role="owner",
            )
        )
        await s.commit()
    yield


# ---------------------------------------------------------------------------
# bsvibe_account_get
# ---------------------------------------------------------------------------
async def test_account_get_create_on_read(db, workspace_id, user_id, registry, seeded) -> None:
    """First call materialises the personal Account row; second call returns same id."""
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        first = await registry.call_tool("bsvibe_account_get", {}, ctx)
    assert first["workspace_id"] == str(workspace_id)
    assert uuid.UUID(first["id"])  # valid uuid

    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        again = await registry.call_tool("bsvibe_account_get", {}, ctx)
    assert again["id"] == first["id"]  # idempotent — same row, no second create


async def test_account_get_requires_read_scope(db, workspace_id, user_id, registry, seeded) -> None:
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=()),
            session=s,
        )
        with pytest.raises(ToolScopeDenied, match="requires scope"):
            await registry.call_tool("bsvibe_account_get", {}, ctx)


# ---------------------------------------------------------------------------
# bsvibe_account_memberships_list
# ---------------------------------------------------------------------------
async def test_memberships_list_returns_active_workspaces(
    db, workspace_id, user_id, registry, seeded
) -> None:
    async with db() as s:
        # Add a SECOND workspace + membership so the list isn't trivially singleton.
        ws2 = uuid.uuid4()
        s.add(WorkspaceRow(id=ws2, name="ws2", region="us-1"))
        await s.flush()
        s.add(
            MembershipRow(
                id=uuid.uuid4(),
                user_id=user_id,
                workspace_id=ws2,
                role="member",
            )
        )
        await s.commit()

    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        out = await registry.call_tool("bsvibe_account_memberships_list", {}, ctx)
    ids = {m["id"] for m in out["memberships"]}
    assert str(workspace_id) in ids
    assert str(ws2) in ids
    assert len(out["memberships"]) == 2
    # Shape parity check — every entry carries the WorkspaceResponse fields.
    sample = out["memberships"][0]
    assert {"id", "name", "region", "safe_mode", "created_at", "updated_at"} <= set(sample)


async def test_memberships_list_excludes_left_memberships(
    db, workspace_id, user_id, registry, seeded
) -> None:
    """A ``left_at``-set membership is NOT returned."""
    from datetime import UTC, datetime

    async with db() as s:
        ws_gone = uuid.uuid4()
        s.add(WorkspaceRow(id=ws_gone, name="gone", region="us-1"))
        await s.flush()
        s.add(
            MembershipRow(
                id=uuid.uuid4(),
                user_id=user_id,
                workspace_id=ws_gone,
                role="member",
                left_at=datetime.now(UTC),
            )
        )
        await s.commit()

    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        out = await registry.call_tool("bsvibe_account_memberships_list", {}, ctx)
    ids = {m["id"] for m in out["memberships"]}
    assert str(ws_gone) not in ids


async def test_memberships_list_requires_read_scope(
    db, workspace_id, user_id, registry, seeded
) -> None:
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=()),
            session=s,
        )
        with pytest.raises(ToolScopeDenied, match="requires scope"):
            await registry.call_tool("bsvibe_account_memberships_list", {}, ctx)


async def test_memberships_list_unknown_user(db, workspace_id, registry, seeded) -> None:
    """An authenticated principal whose ``user_id`` row is absent → wire-safe ToolError."""
    unknown_user = uuid.uuid4()
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(
                workspace_id=workspace_id,
                user_id=unknown_user,
                scopes=("mcp:read",),
            ),
            session=s,
        )
        with pytest.raises(ToolError, match="user not found"):
            await registry.call_tool("bsvibe_account_memberships_list", {}, ctx)
