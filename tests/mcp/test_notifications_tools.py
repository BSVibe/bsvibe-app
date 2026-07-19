"""Notification preference tool handler tests — Lift D3a."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

# Imported for table registration on the shared Base.metadata.
import backend.connectors.db  # noqa: F401
import backend.identity.workspaces_db  # noqa: F401
import backend.notifications.db  # noqa: F401
from backend.config import get_settings
from backend.connectors.db import ConnectorAccountRow
from backend.identity.workspaces_db import WorkspaceRow
from backend.mcp.api import McpPrincipal, ToolContext, ToolRegistry
from backend.mcp.tools import register_all_tools
from backend.notifications.db import default_matrix

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
    async with db() as s:
        s.add(WorkspaceRow(id=workspace_id, name="ws", region="us-1"))
        await s.commit()
    yield


async def test_get_returns_defaults_on_fresh_workspace(
    db, workspace_id, user_id, registry, seeded
) -> None:
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        out = await registry.call_tool("bsvibe_notification_prefs_get", {}, ctx)
    assert set(out["matrix"].keys()) == set(default_matrix().keys())
    assert out["quiet_hours_enabled"] is False
    assert out["quiet_hours_start"] == "22:00"
    assert out["quiet_hours_end"] == "08:00"
    # No connectors bound → only the in_app inbox is an available channel.
    assert out["available_channels"] == ["in_app"]


async def test_get_available_channels_derived_from_telegram_binding(
    db, workspace_id, user_id, registry, seeded
) -> None:
    """[C] MCP mirror — a telegram binding surfaces as an available channel."""
    async with db() as s:
        s.add(
            ConnectorAccountRow(
                workspace_id=workspace_id,
                connector="telegram",
                webhook_token=uuid.uuid4().hex,
                signing_secret_ciphertext="ciphertext",
                delivery_config={"chat_id": "42"},
                is_active=True,
            )
        )
        await s.commit()
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        out = await registry.call_tool("bsvibe_notification_prefs_get", {}, ctx)
    assert out["available_channels"] == ["in_app", "telegram"]


async def test_update_replaces_matrix_wholesale(
    db, workspace_id, user_id, registry, seeded
) -> None:
    new_matrix = default_matrix()
    # Flip everything off for triggered.
    new_matrix["triggered"] = {"in_app": False, "email": False, "slack": False}
    payload = {
        "matrix": new_matrix,
        "quiet_hours_enabled": True,
        "quiet_hours_start": "20:00",
        "quiet_hours_end": "07:00",
    }
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(
                workspace_id=workspace_id,
                user_id=user_id,
                scopes=("mcp:read", "mcp:write"),
            ),
            session=s,
        )
        out = await registry.call_tool("bsvibe_notification_prefs_update", payload, ctx)
    assert out["quiet_hours_enabled"] is True
    assert out["quiet_hours_start"] == "20:00"
    assert out["matrix"]["triggered"]["in_app"] is False

    # Re-read confirms persistence.
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        out2 = await registry.call_tool("bsvibe_notification_prefs_get", {}, ctx)
    assert out2["quiet_hours_enabled"] is True
    assert out2["matrix"]["triggered"]["in_app"] is False


async def test_update_requires_write_scope(db, workspace_id, user_id, registry, seeded) -> None:
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        with pytest.raises(Exception, match="requires scope"):
            await registry.call_tool(
                "bsvibe_notification_prefs_update",
                {
                    "matrix": default_matrix(),
                    "quiet_hours_enabled": False,
                    "quiet_hours_start": "22:00",
                    "quiet_hours_end": "08:00",
                },
                ctx,
            )


async def test_update_rejects_unknown_event(db, workspace_id, user_id, registry, seeded) -> None:
    bad = default_matrix()
    bad["bogus_event"] = {"in_app": True, "email": True, "slack": False}
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(
                workspace_id=workspace_id,
                user_id=user_id,
                scopes=("mcp:read", "mcp:write"),
            ),
            session=s,
        )
        with pytest.raises(Exception, match="matrix events"):
            await registry.call_tool(
                "bsvibe_notification_prefs_update",
                {
                    "matrix": bad,
                    "quiet_hours_enabled": False,
                    "quiet_hours_start": "22:00",
                    "quiet_hours_end": "08:00",
                },
                ctx,
            )
