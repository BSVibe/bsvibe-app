"""Connector tool handler tests — Lift D3a."""

from __future__ import annotations

import base64
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio

# Imported for table registration on the shared Base.metadata.
import backend.connectors.db  # noqa: F401
import backend.identity.db  # noqa: F401
import backend.identity.workspaces_db  # noqa: F401
from backend.config import get_settings
from backend.connectors.db import ConnectorAccountRow
from backend.identity.workspaces_db import WorkspaceRow
from backend.mcp.api import McpPrincipal, ToolContext, ToolError, ToolRegistry
from backend.mcp.tools import register_all_tools

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
async def seeded(db, workspace_id) -> AsyncIterator[None]:
    async with db() as s:
        s.add(WorkspaceRow(id=workspace_id, name="ws", region="us-1"))
        await s.commit()
    yield


async def test_create_lists_show_revoke_round_trip(
    db, workspace_id, user_id, registry, seeded
) -> None:
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
            "bsvibe_connectors_create",
            {
                "connector": "github",
                "signing_secret": "shh-this-is-secret",
                "external_ref": "BSVibe/bsvibe-app",
                "delivery_config": {"repo": "bsvibe-app"},
            },
            ctx,
        )
    assert created["connector"] == "github"
    assert created["kind"] == "outbound"
    # The full token + URL are returned ONLY here.
    assert created["webhook_token"] and len(created["webhook_token"]) > 10
    assert created["webhook_url"].startswith("/api/webhooks/github/")
    new_id = created["id"]

    # List — token is masked
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        listed = await registry.call_tool("bsvibe_connectors_list", {}, ctx)
    assert isinstance(listed, list)
    row = next(r for r in listed if r["id"] == new_id)
    assert row["token_hint"].startswith("...")
    assert "webhook_token" not in row

    # Show
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        shown = await registry.call_tool("bsvibe_connectors_show", {"connector_id": new_id}, ctx)
    assert shown["id"] == new_id
    assert shown["is_active"] is True

    # Delete (soft-revoke)
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(
                workspace_id=workspace_id,
                user_id=user_id,
                scopes=("mcp:read", "mcp:write"),
            ),
            session=s,
        )
        out = await registry.call_tool("bsvibe_connectors_delete", {"connector_id": new_id}, ctx)
    assert out["revoked"] is True

    # Show — still resolves (soft-revoke), but is_active False
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        shown = await registry.call_tool("bsvibe_connectors_show", {"connector_id": new_id}, ctx)
    assert shown["is_active"] is False


async def test_create_requires_write_scope(db, workspace_id, user_id, registry, seeded) -> None:
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        with pytest.raises(Exception, match="requires scope"):
            await registry.call_tool(
                "bsvibe_connectors_create",
                {"connector": "github", "signing_secret": "x"},
                ctx,
            )


async def test_create_rejects_unknown_connector(
    db, workspace_id, user_id, registry, seeded
) -> None:
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(
                workspace_id=workspace_id,
                user_id=user_id,
                scopes=("mcp:read", "mcp:write"),
            ),
            session=s,
        )
        with pytest.raises(ToolError, match="unknown connector"):
            await registry.call_tool(
                "bsvibe_connectors_create",
                {"connector": "not-a-real-connector", "signing_secret": "x"},
                ctx,
            )


async def test_list_scoped_to_workspace(db, workspace_id, user_id, registry, seeded) -> None:
    other_ws = uuid.uuid4()
    async with db() as s:
        s.add(WorkspaceRow(id=other_ws, name="other", region="us-1"))
        await s.flush()
        s.add(
            ConnectorAccountRow(
                id=uuid.uuid4(),
                workspace_id=other_ws,
                connector="slack",
                webhook_token="other-token-aaaa",
                signing_secret_ciphertext="encrypted",
                delivery_config={},
                is_active=True,
            )
        )
        s.add(
            ConnectorAccountRow(
                id=uuid.uuid4(),
                workspace_id=workspace_id,
                connector="discord",
                webhook_token="my-token-bbbb",
                signing_secret_ciphertext="encrypted",
                delivery_config={},
                is_active=True,
            )
        )
        await s.commit()
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        listed = await registry.call_tool("bsvibe_connectors_list", {}, ctx)
    connectors = {r["connector"] for r in listed}
    assert connectors == {"discord"}


class _FakeDispatcher:
    """Test stub — pretends to import 7 notes off an obsidian binding."""

    def __init__(self, *, detail: dict[str, Any]) -> None:
        self.detail = detail
        self.calls = 0

    async def import_for(self, *, row: Any, workspace_id: uuid.UUID) -> dict[str, Any]:
        self.calls += 1
        return self.detail


async def test_import_now_records_telemetry(db, workspace_id, user_id, registry, seeded) -> None:
    """Happy path — dispatcher returns a count, telemetry persists on the row."""
    row_id = uuid.uuid4()
    async with db() as s:
        s.add(
            ConnectorAccountRow(
                id=row_id,
                workspace_id=workspace_id,
                connector="obsidian",
                webhook_token="t" * 16,
                signing_secret_ciphertext="encrypted",
                delivery_config={"vault_path": "/tmp/vault"},
                is_active=True,
            )
        )
        await s.commit()

    fake = _FakeDispatcher(detail={"notes_count": 7, "scanned_count": 9})
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(
                workspace_id=workspace_id,
                user_id=user_id,
                scopes=("mcp:read", "mcp:write"),
            ),
            session=s,
            extras={"import_dispatcher": fake},
        )
        out = await registry.call_tool(
            "bsvibe_connectors_import_now", {"connector_id": str(row_id)}, ctx
        )
    assert out["imported_count"] == 7
    assert out["detail"]["notes_count"] == 7
    assert fake.calls == 1

    # Telemetry persisted
    async with db() as s:
        refreshed = await s.get(ConnectorAccountRow, row_id)
        assert refreshed is not None
        assert refreshed.last_import_count == 7
        assert refreshed.last_import_at is not None


async def test_import_now_rejects_outbound_only(
    db, workspace_id, user_id, registry, seeded
) -> None:
    row_id = uuid.uuid4()
    async with db() as s:
        s.add(
            ConnectorAccountRow(
                id=row_id,
                workspace_id=workspace_id,
                connector="telegram",
                webhook_token="t" * 16,
                signing_secret_ciphertext="encrypted",
                delivery_config={},
                is_active=True,
            )
        )
        await s.commit()
    fake = _FakeDispatcher(detail={})
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(
                workspace_id=workspace_id,
                user_id=user_id,
                scopes=("mcp:read", "mcp:write"),
            ),
            session=s,
            extras={"import_dispatcher": fake},
        )
        with pytest.raises(ToolError, match="outbound-only"):
            await registry.call_tool(
                "bsvibe_connectors_import_now", {"connector_id": str(row_id)}, ctx
            )
    assert fake.calls == 0


async def test_import_now_requires_write_scope(db, workspace_id, user_id, registry, seeded) -> None:
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        with pytest.raises(Exception, match="requires scope"):
            await registry.call_tool(
                "bsvibe_connectors_import_now",
                {"connector_id": str(uuid.uuid4())},
                ctx,
            )
