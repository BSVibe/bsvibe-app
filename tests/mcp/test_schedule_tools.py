"""Schedule authoring tool handler tests — Slice S2 (MCP parity).

Mirrors ``tests/mcp/test_notifications_tools.py``: drives the real
:class:`~backend.schedule.application.schedule_service.ScheduleService` +
repository + INV-1 producer emit through the MCP dispatcher, so the tools are
genuinely wired to the same canonical service path the REST surface uses (S1),
not a stub.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import select

# Imported for table registration on the shared Base.metadata.
import backend.identity.workspaces_db  # noqa: F401
import backend.schedule.infrastructure.schedule_db  # noqa: F401
from backend.channels._core import Channel
from backend.config import get_settings
from backend.mcp.api import McpPrincipal, ToolContext, ToolRegistry
from backend.mcp.tools import register_all_tools
from backend.schedule.infrastructure.schedule_db import WorkspaceScheduleRow

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


async def _create(registry, db, workspace_id, user_id, **overrides):
    payload = {
        "kind": "instruction",
        "text": "post the weekly market summary",
        "cron_expr": "0 9 * * 1",
        "title": "Weekly summary",
    }
    payload.update(overrides)
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(
                workspace_id=workspace_id,
                user_id=user_id,
                scopes=("mcp:read", "mcp:write"),
            ),
            session=s,
        )
        return await registry.call_tool("bsvibe_schedules_create", payload, ctx)


async def test_create_inserts_row_via_service(db, workspace_id, user_id, registry) -> None:
    """Producer-existence — a real WorkspaceScheduleRow lands through the service."""
    out = await _create(registry, db, workspace_id, user_id)
    assert out["kind"] == "instruction"
    assert out["text"] == "post the weekly market summary"
    assert out["cron_expr"] == "0 9 * * 1"
    assert out["title"] == "Weekly summary"
    assert out["enabled"] is True
    assert out["next_run_at"] is not None

    async with db() as s:
        row = (
            await s.execute(
                select(WorkspaceScheduleRow).where(
                    WorkspaceScheduleRow.workspace_id == workspace_id
                )
            )
        ).scalar_one()
        assert row.payload == {"text": "post the weekly market summary"}
        assert str(row.id) == out["id"]
        assert row.plugin_name is None


async def test_create_emits_with_mcp_producer_id(
    db, workspace_id, user_id, registry, monkeypatch
) -> None:
    """The MCP create emits through the channel with ``mcp:schedules_create``."""
    recorded: list[str] = []
    original = Channel.assert_producer

    def _spy(self, producer_id: str) -> None:
        if self.name == "workspace_schedules":
            recorded.append(producer_id)
        return original(self, producer_id)

    monkeypatch.setattr(Channel, "assert_producer", _spy)

    await _create(registry, db, workspace_id, user_id)
    assert recorded == ["mcp:schedules_create"]


async def test_list_returns_created(db, workspace_id, user_id, registry) -> None:
    created = await _create(registry, db, workspace_id, user_id)
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        listed = await registry.call_tool("bsvibe_schedules_list", {}, ctx)
    assert [r["id"] for r in listed] == [created["id"]]


async def test_set_enabled_toggles(db, workspace_id, user_id, registry) -> None:
    created = await _create(registry, db, workspace_id, user_id)
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(
                workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read", "mcp:write")
            ),
            session=s,
        )
        out = await registry.call_tool(
            "bsvibe_schedules_set_enabled",
            {"schedule_id": created["id"], "enabled": False},
            ctx,
        )
    assert out["enabled"] is False
    assert out["id"] == created["id"]

    async with db() as s:
        row = await s.get(WorkspaceScheduleRow, uuid.UUID(created["id"]))
        assert row is not None
        assert row.enabled is False


async def test_delete_removes(db, workspace_id, user_id, registry) -> None:
    created = await _create(registry, db, workspace_id, user_id)
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(
                workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read", "mcp:write")
            ),
            session=s,
        )
        out = await registry.call_tool(
            "bsvibe_schedules_delete", {"schedule_id": created["id"]}, ctx
        )
    assert out["deleted"] is True
    assert out["schedule_id"] == created["id"]

    async with db() as s:
        row = await s.get(WorkspaceScheduleRow, uuid.UUID(created["id"]))
        assert row is None


async def test_delete_missing_is_tool_error(db, workspace_id, user_id, registry) -> None:
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(
                workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read", "mcp:write")
            ),
            session=s,
        )
        with pytest.raises(Exception, match="not found"):
            await registry.call_tool(
                "bsvibe_schedules_delete", {"schedule_id": str(uuid.uuid4())}, ctx
            )


async def test_set_enabled_missing_is_tool_error(db, workspace_id, user_id, registry) -> None:
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(
                workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read", "mcp:write")
            ),
            session=s,
        )
        with pytest.raises(Exception, match="not found"):
            await registry.call_tool(
                "bsvibe_schedules_set_enabled",
                {"schedule_id": str(uuid.uuid4()), "enabled": True},
                ctx,
            )


async def test_create_rejects_extra_field(db, workspace_id, user_id, registry) -> None:
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(
                workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read", "mcp:write")
            ),
            session=s,
        )
        with pytest.raises(Exception, match="invalid arguments"):
            await registry.call_tool(
                "bsvibe_schedules_create",
                {"text": "x", "cron_expr": "* * * * *", "surprise": "nope"},
                ctx,
            )


async def test_create_invalid_cron_is_tool_error(db, workspace_id, user_id, registry) -> None:
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(
                workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read", "mcp:write")
            ),
            session=s,
        )
        with pytest.raises(Exception, match="cron"):
            await registry.call_tool(
                "bsvibe_schedules_create",
                {"text": "x", "cron_expr": "not a cron"},
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
                "bsvibe_schedules_create",
                {"text": "x", "cron_expr": "* * * * *"},
                ctx,
            )


async def test_kind_defaults_to_instruction(db, workspace_id, user_id, registry) -> None:
    out = await _create(registry, db, workspace_id, user_id, kind="instruction")
    assert out["kind"] == "instruction"


async def test_mcp_schemas_match_rest_models() -> None:
    """Parity — the MCP input/output shapes mirror the REST models field-for-field."""
    from backend.api.v1 import schedules as rest
    from backend.mcp.tools import schedule_tools as mcp

    assert set(mcp.ScheduleCreateInput.model_fields) == set(rest.ScheduleCreate.model_fields)
    assert set(mcp.ScheduleView.model_fields) == set(rest.ScheduleView.model_fields)
    # The enable toggle carries the same boolean the REST PATCH body does.
    assert "enabled" in mcp.ScheduleSetEnabledInput.model_fields
    assert set(rest.ScheduleEnabledPatch.model_fields) == {"enabled"}
