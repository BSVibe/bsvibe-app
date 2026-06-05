"""Inside Trust tool handler tests — Lift D3d.

The :class:`TrustSurfaceService` has its own deep coverage under
``tests/workflow/application/metrics``; the wrapper tests here verify
the MCP shape contract:

* scope rejection (``mcp:read`` absent → ``ToolError``),
* empty workspace returns ``{products: []}`` (mirrors REST),
* a missing-product show call still returns the dormant shape (no
  ``ToolError`` — matches the REST "never 404 for an unknown product
  id" contract).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

# Imported for table registration on the shared Base.metadata.
import backend.identity.db  # noqa: F401
import backend.identity.workspaces_db  # noqa: F401
import backend.workers.db  # noqa: F401 — registers SettleDrainRow
import backend.workflow.infrastructure.db  # noqa: F401 — registers ExecutionRun / Decision
from backend.config import get_settings
from backend.identity.workspaces_db import WorkspaceRow
from backend.mcp.api import McpPrincipal, ToolContext, ToolRegistry, ToolScopeDenied
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
    """Stage-flush the workspace parent so PG FKs resolve."""
    async with db() as s:
        s.add(WorkspaceRow(id=workspace_id, name="ws", region="us-1"))
        await s.commit()
    yield


async def test_fleet_empty_workspace_returns_empty(
    db, workspace_id, user_id, registry, seeded
) -> None:
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        out = await registry.call_tool("bsvibe_inside_trust_fleet", {}, ctx)
    assert out == {"products": []}


async def test_fleet_requires_read_scope(db, workspace_id, user_id, registry, seeded) -> None:
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=()),
            session=s,
        )
        with pytest.raises(ToolScopeDenied, match="requires scope"):
            await registry.call_tool("bsvibe_inside_trust_fleet", {}, ctx)


async def test_show_unknown_product_returns_dormant_shape(
    db, workspace_id, user_id, registry, seeded
) -> None:
    """No runs / drains / decisions for the product → dormant glyph + zeros.

    Mirrors the REST contract: a product the service has never seen does
    NOT 404 — it returns the same shape with zeros. The L3 Inside trust
    strip can render a brand-new product without a conditional.
    """
    product_id = uuid.uuid4()
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        out = await registry.call_tool(
            "bsvibe_inside_trust_show", {"product_id": str(product_id)}, ctx
        )
    assert out["product_id"] == str(product_id)
    assert out["touch_time"]["decisions_resolved_count"] == 0
    assert out["touch_time"]["decisions_pending_count"] == 0
    assert out["deposit_rate"]["deposit_count"] == 0
    # Dormant glyph per design Q7 — min-data threshold not met.
    assert out["trend_arrow"]["glyph"] == "·"
    assert out["contract_strength"]["is_steady"] is True


async def test_show_requires_read_scope(db, workspace_id, user_id, registry, seeded) -> None:
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=()),
            session=s,
        )
        with pytest.raises(ToolScopeDenied, match="requires scope"):
            await registry.call_tool(
                "bsvibe_inside_trust_show", {"product_id": str(uuid.uuid4())}, ctx
            )
