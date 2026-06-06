"""Resource binding tool handler tests — Lift D3b."""

from __future__ import annotations

import base64
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

# Imported for table registration on the shared Base.metadata.
import backend.connectors.db  # noqa: F401
import backend.identity.db  # noqa: F401
import backend.identity.workspaces_db  # noqa: F401
from backend.config import get_settings
from backend.connectors.db import ConnectorAccountRow
from backend.identity.workspaces_db import ProductRow, ResourceBindingRow, WorkspaceRow
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
async def seeded(db, workspace_id) -> AsyncIterator[tuple[uuid.UUID, uuid.UUID]]:
    """Seed a workspace + product + connector_account; yield their ids.

    Stage-flush the workspace BEFORE inserting child rows so the FK
    references resolve under real Postgres
    (sqlalchemy-test-fixture-pg-fk-insert-order skill).
    """
    product_id = uuid.uuid4()
    connector_account_id = uuid.uuid4()
    async with db() as s:
        s.add(WorkspaceRow(id=workspace_id, name="ws", region="us-1"))
        await s.flush()
        s.add(ProductRow(id=product_id, workspace_id=workspace_id, name="P", slug="p"))
        s.add(
            ConnectorAccountRow(
                id=connector_account_id,
                workspace_id=workspace_id,
                connector="github",
                webhook_token="t" * 16,
                signing_secret_ciphertext="encrypted",
                delivery_config={},
                is_active=True,
            )
        )
        await s.commit()
    yield (product_id, connector_account_id)


async def test_create_list_update_delete_round_trip(
    db, workspace_id, user_id, registry, seeded
) -> None:
    product_id, connector_account_id = seeded

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
            "bsvibe_bindings_create",
            {
                "product_id": str(product_id),
                "connector_account_id": str(connector_account_id),
                "resource_id": "BSVibe/bsvibe-site",
                "output_mode": "safe",
            },
            ctx,
        )
    assert created["product_id"] == str(product_id)
    assert created["resource_id"] == "BSVibe/bsvibe-site"
    assert created["output_mode"] == "safe"
    binding_id = created["id"]

    # List scoped to product
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        listed = await registry.call_tool(
            "bsvibe_bindings_list", {"product_id": str(product_id)}, ctx
        )
    assert isinstance(listed, list)
    assert any(r["id"] == binding_id for r in listed)

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
            "bsvibe_bindings_update",
            {
                "binding_id": binding_id,
                "output_mode": "direct",
                "trigger": {"enabled": True, "filters": {"labels": ["bug"]}},
            },
            ctx,
        )
    assert updated["output_mode"] == "direct"
    assert updated["trigger"]["enabled"] is True

    # Delete
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(
                workspace_id=workspace_id,
                user_id=user_id,
                scopes=("mcp:read", "mcp:write"),
            ),
            session=s,
        )
        out = await registry.call_tool("bsvibe_bindings_delete", {"binding_id": binding_id}, ctx)
    assert out["deleted"] is True


async def test_create_requires_write_scope(db, workspace_id, user_id, registry, seeded) -> None:
    product_id, connector_account_id = seeded
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        with pytest.raises(Exception, match="requires scope"):
            await registry.call_tool(
                "bsvibe_bindings_create",
                {
                    "product_id": str(product_id),
                    "connector_account_id": str(connector_account_id),
                    "resource_id": "x/y",
                },
                ctx,
            )


async def test_create_rejects_invalid_output_mode(
    db, workspace_id, user_id, registry, seeded
) -> None:
    product_id, connector_account_id = seeded
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(
                workspace_id=workspace_id,
                user_id=user_id,
                scopes=("mcp:read", "mcp:write"),
            ),
            session=s,
        )
        with pytest.raises(ToolError, match="output_mode"):
            await registry.call_tool(
                "bsvibe_bindings_create",
                {
                    "product_id": str(product_id),
                    "connector_account_id": str(connector_account_id),
                    "resource_id": "x/y",
                    "output_mode": "loud",
                },
                ctx,
            )


async def test_create_rejects_product_in_other_workspace(
    db, workspace_id, user_id, registry, seeded
) -> None:
    """Cross-workspace product → ToolError, no row written."""
    _, connector_account_id = seeded
    other_ws = uuid.uuid4()
    other_product = uuid.uuid4()
    async with db() as s:
        s.add(WorkspaceRow(id=other_ws, name="other", region="us-1"))
        await s.flush()
        s.add(ProductRow(id=other_product, workspace_id=other_ws, name="X", slug="x"))
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
        with pytest.raises(ToolError, match="product not found"):
            await registry.call_tool(
                "bsvibe_bindings_create",
                {
                    "product_id": str(other_product),
                    "connector_account_id": str(connector_account_id),
                    "resource_id": "x/y",
                },
                ctx,
            )


async def test_list_workspace_scoped(db, workspace_id, user_id, registry, seeded) -> None:
    """List with no product_id returns only bindings in caller's workspace."""
    product_id, connector_account_id = seeded
    other_ws = uuid.uuid4()
    other_product = uuid.uuid4()
    other_account = uuid.uuid4()
    async with db() as s:
        s.add(WorkspaceRow(id=other_ws, name="other", region="us-1"))
        await s.flush()
        s.add(ProductRow(id=other_product, workspace_id=other_ws, name="X", slug="x"))
        s.add(
            ConnectorAccountRow(
                id=other_account,
                workspace_id=other_ws,
                connector="github",
                webhook_token="o" * 16,
                signing_secret_ciphertext="encrypted",
                delivery_config={},
                is_active=True,
            )
        )
        await s.flush()
        s.add(
            ResourceBindingRow(
                workspace_id=workspace_id,
                product_id=product_id,
                connector_account_id=connector_account_id,
                resource_id="mine/x",
            )
        )
        s.add(
            ResourceBindingRow(
                workspace_id=other_ws,
                product_id=other_product,
                connector_account_id=other_account,
                resource_id="other/y",
            )
        )
        await s.commit()
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        out = await registry.call_tool("bsvibe_bindings_list", {}, ctx)
    resource_ids = {r["resource_id"] for r in out}
    assert resource_ids == {"mine/x"}


async def test_delete_requires_write_scope(db, workspace_id, user_id, registry, seeded) -> None:
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        with pytest.raises(Exception, match="requires scope"):
            await registry.call_tool(
                "bsvibe_bindings_delete", {"binding_id": str(uuid.uuid4())}, ctx
            )
