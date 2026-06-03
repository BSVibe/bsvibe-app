"""Safe Mode + Direct tool handler tests — Lift D2."""

from __future__ import annotations

import base64
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
import pytest_asyncio

# Imported for table registration on the shared Base.metadata.
import backend.identity.db  # noqa: F401
import backend.identity.workspaces_db  # noqa: F401
import backend.workflow.infrastructure.db  # noqa: F401
import backend.workflow.infrastructure.delivery.db  # noqa: F401
import backend.workflow.infrastructure.intake.db  # noqa: F401
from backend.config import get_settings
from backend.identity.db import UserRow
from backend.identity.workspaces_db import ProductRow, WorkspaceRow
from backend.mcp.api import McpPrincipal, ToolContext, ToolError, ToolRegistry
from backend.mcp.tools import register_all_tools
from backend.workflow.application.safe_mode_queue import SafeModeQueue
from backend.workflow.infrastructure.db import (
    Deliverable,
    DeliverableType,
    ExecutionRun,
    RunStatus,
)

from .._support import db_engine

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def db(monkeypatch) -> AsyncIterator:
    monkeypatch.setenv(
        "BSVIBE_GATEWAY_KMS_KEY_B64",
        base64.urlsafe_b64encode(b"0" * 32).decode(),
    )
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
async def seeded(db, workspace_id, user_id) -> AsyncIterator[uuid.UUID]:
    """Seed a workspace + user + run + deliverable + queue item; yield item_id."""
    item_id: uuid.UUID | None = None
    async with db() as s:
        s.add(WorkspaceRow(id=workspace_id, name="ws", region="us-1"))
        s.add(UserRow(id=user_id, supabase_user_id="test-user", email="t@example.com"))
        run = ExecutionRun(
            id=uuid.uuid4(),
            workspace_id=workspace_id,
            product_id=None,
            status=RunStatus.RUNNING,
            payload={},
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        s.add(run)
        deliverable = Deliverable(
            id=uuid.uuid4(),
            run_id=run.id,
            workspace_id=workspace_id,
            deliverable_type=DeliverableType.DIRECT_OUTPUT,
            payload={},
        )
        s.add(deliverable)
        await s.flush()
        queue = SafeModeQueue(s)
        item_id = await queue.enqueue(
            workspace_id=workspace_id,
            deliverable_id=deliverable.id,
            run_id=run.id,
        )
        await s.commit()
    assert item_id is not None
    yield item_id


async def test_safe_mode_list_pending_returns_seeded_item(
    db, workspace_id, user_id, registry, seeded
) -> None:
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        out = await registry.call_tool("bsvibe_safe_mode_list_pending", {}, ctx)
    assert out["total"] == 1
    assert out["items"][0]["id"] == str(seeded)
    assert out["items"][0]["status"] == "pending"


async def test_safe_mode_approve_flips_state(db, workspace_id, user_id, registry, seeded) -> None:
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(
                workspace_id=workspace_id,
                user_id=user_id,
                scopes=("mcp:read", "mcp:write"),
            ),
            session=s,
        )
        out = await registry.call_tool("bsvibe_safe_mode_approve", {"item_id": str(seeded)}, ctx)
    assert out["status"] == "approved"
    # Verify pending list is now empty.
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        listed = await registry.call_tool("bsvibe_safe_mode_list_pending", {}, ctx)
    assert listed["total"] == 0


async def test_safe_mode_deny_flips_state(db, workspace_id, user_id, registry, seeded) -> None:
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(
                workspace_id=workspace_id,
                user_id=user_id,
                scopes=("mcp:read", "mcp:write"),
            ),
            session=s,
        )
        out = await registry.call_tool(
            "bsvibe_safe_mode_deny",
            {"item_id": str(seeded), "reason": "wrong target"},
            ctx,
        )
    assert out["status"] == "denied"


async def test_safe_mode_approve_requires_write_scope(
    db, workspace_id, user_id, registry, seeded
) -> None:
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        with pytest.raises(Exception, match="requires scope"):
            await registry.call_tool("bsvibe_safe_mode_approve", {"item_id": str(seeded)}, ctx)


async def test_safe_mode_approve_unknown_item_raises(
    db, workspace_id, user_id, registry, seeded
) -> None:
    bogus = uuid.uuid4()
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(
                workspace_id=workspace_id,
                user_id=user_id,
                scopes=("mcp:read", "mcp:write"),
            ),
            session=s,
        )
        with pytest.raises(ToolError, match="no pending"):
            await registry.call_tool("bsvibe_safe_mode_approve", {"item_id": str(bogus)}, ctx)


async def test_direct_requires_a_product(db, workspace_id, user_id, registry) -> None:
    async with db() as s:
        s.add(WorkspaceRow(id=workspace_id, name="ws", region="us-1"))
        s.add(UserRow(id=user_id, supabase_user_id="t", email="t@e.co"))
        await s.commit()
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(
                workspace_id=workspace_id,
                user_id=user_id,
                scopes=("mcp:read", "mcp:write"),
            ),
            session=s,
        )
        with pytest.raises(ToolError, match="no products"):
            await registry.call_tool("bsvibe_direct", {"text": "do a thing"}, ctx)


async def test_direct_accepts_with_product(
    db, workspace_id, user_id, registry, monkeypatch
) -> None:
    # Stub the emit so the test doesn't actually try to reach Redis.
    monkeypatch.setattr(
        "backend.mcp.tools.direct_tools.emit_stream_notification",
        lambda *args, **kw: _noop(),
    )

    async def _noop_async(*args, **kw):
        return None

    monkeypatch.setattr("backend.mcp.tools.direct_tools.emit_stream_notification", _noop_async)
    monkeypatch.setattr(
        "backend.mcp.tools.direct_tools.get_emit_redis_client", lambda settings: None
    )
    async with db() as s:
        s.add(WorkspaceRow(id=workspace_id, name="ws", region="us-1"))
        s.add(UserRow(id=user_id, supabase_user_id="t", email="t@e.co"))
        s.add(ProductRow(workspace_id=workspace_id, name="A", slug="a"))
        await s.commit()
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(
                workspace_id=workspace_id,
                user_id=user_id,
                scopes=("mcp:read", "mcp:write"),
            ),
            session=s,
        )
        out = await registry.call_tool("bsvibe_direct", {"text": "fix the thing"}, ctx)
    assert out["accepted"] is True
    assert out["workspace_id"] == str(workspace_id)


def _noop() -> None:
    return None


async def test_direct_requires_write_scope(db, workspace_id, user_id, registry) -> None:
    async with db() as s:
        s.add(WorkspaceRow(id=workspace_id, name="ws", region="us-1"))
        s.add(UserRow(id=user_id, supabase_user_id="t", email="t@e.co"))
        s.add(ProductRow(workspace_id=workspace_id, name="A", slug="a"))
        await s.commit()
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        with pytest.raises(Exception, match="requires scope"):
            await registry.call_tool("bsvibe_direct", {"text": "x"}, ctx)
