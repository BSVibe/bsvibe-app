"""Safe Mode + Direct tool handler tests — Lift D2."""

from __future__ import annotations

import base64
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

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
        await s.flush()
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
        await s.flush()
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
            extras={"delivery_dispatcher": _StubDeliveryDispatcher()},
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


class _StubDeliveryDispatcher:
    """Test seam — records every dispatch call so tests can assert the MCP
    approve path actually ran the outbound dispatch (E40 parity fix). The
    duck-typed contract matches
    :class:`~backend.workflow.infrastructure.workers.delivery_worker.PluginDispatchAdapter`.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.result_actions: int = 1

    async def dispatch(self, *, workspace_id, deliverable_id, artifact_type):
        from backend.workflow.domain.delivery import ActionResult, DeliveryResult

        self.calls.append(
            {
                "workspace_id": workspace_id,
                "deliverable_id": deliverable_id,
                "artifact_type": artifact_type,
            }
        )
        return DeliveryResult(
            workspace_id=workspace_id,
            deliverable_id=deliverable_id,
            artifact_type=artifact_type,
            actions=[
                ActionResult(action=f"stub:outbound:{artifact_type}", succeeded=True, output={})
                for _ in range(self.result_actions)
            ],
        )


async def test_safe_mode_approve_dispatches_via_injected_dispatcher(
    db, workspace_id, user_id, registry, seeded
) -> None:
    """Lift E40 — MCP `bsvibe_safe_mode_approve` MUST mirror the REST
    `POST /api/v1/safemode/{id}/approve` parity ([[bsvibe-mcp-ui-parity]]):
    approve flips the queue row AND runs the outbound dispatch through the
    SAME `dispatch_delivery` helper the REST route uses. Pre-E40 the MCP
    handler only flipped the queue row and returned `dispatched=False`,
    relying on "the worker's next tick re-drains" — but the worker drains
    `delivery_events`, not the safe_mode queue, so the approved item never
    dispatched and the deliverable's `diff_url` stayed NULL forever.

    The dogfood retrace (run 1079bff5, 2026-06-17) caught this: the run
    reached `review_ready`, the MCP tool flipped the queue row, but the
    PR never opened.
    """
    dispatcher = _StubDeliveryDispatcher()
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(
                workspace_id=workspace_id,
                user_id=user_id,
                scopes=("mcp:read", "mcp:write"),
            ),
            session=s,
            extras={"delivery_dispatcher": dispatcher},
        )
        out = await registry.call_tool("bsvibe_safe_mode_approve", {"item_id": str(seeded)}, ctx)

    assert out["status"] == "approved"
    # E40 — the output now reflects the actual dispatch.
    assert out["dispatched"] is True
    # Dispatcher was hit exactly once for the approved item's deliverable.
    assert len(dispatcher.calls) == 1
    assert dispatcher.calls[0]["workspace_id"] == workspace_id
    assert dispatcher.calls[0]["artifact_type"] == "direct_output"


async def test_safe_mode_approve_dispatch_failure_does_not_revert_approval(
    db, workspace_id, user_id, registry, seeded
) -> None:
    """Lift E40 — approval is irreversible (mirrors PWA + REST behaviour).
    A transient dispatch failure must still leave the queue item in
    ``approved`` state — the founder can retry the outbound side later.
    """

    class _FailingDispatcher:
        async def dispatch(self, *, workspace_id, deliverable_id, artifact_type):
            raise RuntimeError("connector unavailable")

    async with db() as s:
        ctx = ToolContext(
            principal=_principal(
                workspace_id=workspace_id,
                user_id=user_id,
                scopes=("mcp:read", "mcp:write"),
            ),
            session=s,
            extras={"delivery_dispatcher": _FailingDispatcher()},
        )
        out = await registry.call_tool("bsvibe_safe_mode_approve", {"item_id": str(seeded)}, ctx)

    # Approval succeeded; dispatched flag reflects the failure.
    assert out["status"] == "approved"
    assert out["dispatched"] is False
    # The queue is empty — the item has flipped past pending regardless.
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
        await s.flush()
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
        await s.flush()
        s.add(ProductRow(workspace_id=workspace_id, name="A", slug="a"))
        await s.commit()
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        with pytest.raises(Exception, match="requires scope"):
            await registry.call_tool("bsvibe_direct", {"text": "x"}, ctx)
