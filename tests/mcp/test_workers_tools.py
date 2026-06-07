"""Workers tool handler tests — Lift E4."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

# Imported for table registration on the shared Base.metadata.
import backend.executors.db  # noqa: F401
import backend.identity.db  # noqa: F401
import backend.identity.workspaces_db  # noqa: F401
import backend.router.accounts.account_models  # noqa: F401
import backend.router.accounts.models  # noqa: F401
from backend.executors import service
from backend.identity.workspaces_db import WorkspaceRow
from backend.mcp.api import McpPrincipal, ToolContext, ToolError, ToolRegistry
from backend.mcp.tools import register_all_tools

from .._support import db_engine

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def db() -> AsyncIterator:
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


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
    async with db() as s:
        s.add(WorkspaceRow(id=workspace_id, name="ws", region="us-1"))
        await s.commit()
    yield


async def test_workers_list_returns_workspace_workers(
    db, workspace_id, user_id, registry, seeded
) -> None:
    # Seed a worker through the service so the routable executor row gets made.
    async with db() as s:
        await service.register_worker_for_workspace(
            s,
            workspace_id=workspace_id,
            name="mac-mini",
            labels=["m4"],
            capabilities=["claude_code"],
        )
        await s.commit()

    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        out = await registry.call_tool("bsvibe_workers_list", {}, ctx)
    assert isinstance(out, list)
    assert len(out) == 1
    assert out[0]["name"] == "mac-mini"
    assert out[0]["capabilities"] == ["claude_code"]
    assert out[0]["status"] == "offline"
    assert out[0]["created_at"]


async def test_workers_revoke_marks_inactive(db, workspace_id, user_id, registry, seeded) -> None:
    async with db() as s:
        worker, _ = await service.register_worker_for_workspace(
            s,
            workspace_id=workspace_id,
            name="doomed",
            labels=[],
            capabilities=[],
        )
        await s.commit()
        worker_id = worker.id

    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:write",)),
            session=s,
        )
        out = await registry.call_tool(
            "bsvibe_workers_revoke",
            {"worker_id": str(worker_id)},
            ctx,
        )
    assert out == {"revoked": True, "worker_id": str(worker_id)}

    # List is now empty.
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        listed = await registry.call_tool("bsvibe_workers_list", {}, ctx)
    assert listed == []


async def test_workers_revoke_404_when_not_found(
    db, workspace_id, user_id, registry, seeded
) -> None:
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:write",)),
            session=s,
        )
        with pytest.raises(ToolError, match="worker not found"):
            await registry.call_tool(
                "bsvibe_workers_revoke",
                {"worker_id": str(uuid.uuid4())},
                ctx,
            )


async def test_workers_revoke_denied_without_write_scope(
    db, workspace_id, user_id, registry, seeded
) -> None:
    """read-only scope cannot revoke."""
    from backend.mcp.api import ToolScopeDenied

    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        with pytest.raises(ToolScopeDenied):
            await registry.call_tool(
                "bsvibe_workers_revoke",
                {"worker_id": str(uuid.uuid4())},
                ctx,
            )


async def test_workers_list_exposes_e13_fields(db, workspace_id, user_id, registry, seeded) -> None:
    """Lift E13 — list output carries capabilities, labels, last_heartbeat,
    heartbeat_fresh, status, is_active, created_at — the fleet-detail shape
    the founder UI + ``find_available_worker`` debugging needs.
    """
    from datetime import UTC, datetime, timedelta

    from backend.executors.db import WorkerRow
    from backend.executors.dispatch import HEARTBEAT_FRESHNESS_S

    fresh_id = uuid.uuid4()
    stale_id = uuid.uuid4()
    silent_id = uuid.uuid4()
    now = datetime.now(UTC)

    async with db() as s:
        # Fresh online — under the freshness cutoff.
        s.add(
            WorkerRow(
                id=fresh_id,
                workspace_id=workspace_id,
                name="fresh",
                labels=["mac"],
                capabilities=["codex", "opencode"],
                status="online",
                is_active=True,
                last_heartbeat=now - timedelta(seconds=5),
                token_hash="h1",
            )
        )
        # Stale online — claims online but heartbeat well past the cutoff.
        s.add(
            WorkerRow(
                id=stale_id,
                workspace_id=workspace_id,
                name="stale",
                labels=[],
                capabilities=["claude_code"],
                status="online",
                is_active=True,
                last_heartbeat=now - timedelta(seconds=HEARTBEAT_FRESHNESS_S + 60),
                token_hash="h2",
            )
        )
        # Never heartbeated.
        s.add(
            WorkerRow(
                id=silent_id,
                workspace_id=workspace_id,
                name="silent",
                labels=["new"],
                capabilities=[],
                status="offline",
                is_active=True,
                last_heartbeat=None,
                token_hash="h3",
            )
        )
        await s.commit()

    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        out = await registry.call_tool("bsvibe_workers_list", {}, ctx)

    by_name = {w["name"]: w for w in out}
    assert set(by_name) == {"fresh", "stale", "silent"}

    fresh = by_name["fresh"]
    assert fresh["capabilities"] == ["codex", "opencode"]
    assert fresh["labels"] == ["mac"]
    assert fresh["status"] == "online"
    assert fresh["is_active"] is True
    assert fresh["last_heartbeat"] is not None
    assert fresh["heartbeat_fresh"] is True
    assert fresh["created_at"]

    stale = by_name["stale"]
    # status row says online but heartbeat is stale — surfaces as False.
    assert stale["status"] == "online"
    assert stale["heartbeat_fresh"] is False
    assert stale["last_heartbeat"] is not None

    silent = by_name["silent"]
    assert silent["last_heartbeat"] is None
    assert silent["heartbeat_fresh"] is False


async def test_workers_list_isolates_workspaces(db, registry, seeded) -> None:
    other_workspace = uuid.uuid4()
    async with db() as s:
        s.add(WorkspaceRow(id=other_workspace, name="other", region="us-1"))
        await s.commit()

    async with db() as s:
        await service.register_worker_for_workspace(
            s,
            workspace_id=other_workspace,
            name="other-only",
            labels=[],
            capabilities=[],
        )
        await s.commit()

    # Original workspace sees nothing.
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(
                workspace_id=uuid.uuid4(), user_id=uuid.uuid4(), scopes=("mcp:read",)
            ),
            session=s,
        )
        listed = await registry.call_tool("bsvibe_workers_list", {}, ctx)
    assert listed == []
