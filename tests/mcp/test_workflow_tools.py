"""Workflow tool handler tests — Lift D2.

Exercises products / runs / deliverables tools end-to-end against an
in-memory SQLite DB. Each test seeds the row(s) it needs, constructs a
:class:`ToolContext` with a deterministic :class:`McpPrincipal`, calls
the tool, and asserts the typed output shape.
"""

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
from backend.config import get_settings
from backend.identity.workspaces_db import ProductRow, WorkspaceRow
from backend.mcp.api import McpPrincipal, ToolContext, ToolError, ToolRegistry
from backend.mcp.tools import register_all_tools
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
async def seeded(db, workspace_id) -> AsyncIterator[None]:
    async with db() as s:
        ws = WorkspaceRow(id=workspace_id, name="ws", region="us-1")
        s.add(ws)
        await s.commit()
        yield


async def test_products_list_returns_workspace_scoped_rows(
    db, workspace_id, user_id, registry, seeded
) -> None:
    other_ws = uuid.uuid4()
    async with db() as s:
        s.add(WorkspaceRow(id=other_ws, name="other", region="us-1"))
        await s.flush()
        s.add(ProductRow(workspace_id=workspace_id, name="A", slug="a"))
        s.add(ProductRow(workspace_id=workspace_id, name="B", slug="b"))
        s.add(ProductRow(workspace_id=other_ws, name="X", slug="x"))
        await s.commit()
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        out = await registry.call_tool("bsvibe_products_list", {"limit": 50}, ctx)
    assert isinstance(out, list)
    slugs = {p["slug"] for p in out}
    assert slugs == {"a", "b"}


async def test_products_show_by_slug_and_uuid(db, workspace_id, user_id, registry, seeded) -> None:
    pid = uuid.uuid4()
    async with db() as s:
        s.add(ProductRow(id=pid, workspace_id=workspace_id, name="A", slug="a"))
        await s.commit()
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        by_slug = await registry.call_tool("bsvibe_products_show", {"slug_or_id": "a"}, ctx)
        by_uuid = await registry.call_tool("bsvibe_products_show", {"slug_or_id": str(pid)}, ctx)
    assert by_slug["slug"] == "a"
    assert by_uuid["slug"] == "a"
    assert by_uuid["id"] == str(pid)


async def test_products_show_other_workspace_not_found(
    db, workspace_id, user_id, registry, seeded
) -> None:
    other_ws = uuid.uuid4()
    async with db() as s:
        s.add(WorkspaceRow(id=other_ws, name="other", region="us-1"))
        await s.flush()
        s.add(ProductRow(workspace_id=other_ws, name="X", slug="x"))
        await s.commit()
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        with pytest.raises(ToolError, match="product not found"):
            await registry.call_tool("bsvibe_products_show", {"slug_or_id": "x"}, ctx)


async def test_products_create_requires_write_scope(
    db, workspace_id, user_id, registry, seeded
) -> None:
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        with pytest.raises(Exception, match="requires scope"):
            await registry.call_tool("bsvibe_products_create", {"name": "A", "slug": "a"}, ctx)


async def test_products_create_writes_row(db, workspace_id, user_id, registry, seeded) -> None:
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
            "bsvibe_products_create",
            {"name": "MCP Created", "slug": "mcp-created"},
            ctx,
        )
    assert out["slug"] == "mcp-created"
    assert out["name"] == "MCP Created"
    # Verify the row landed.
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        listed = await registry.call_tool("bsvibe_products_list", {}, ctx)
    assert any(p["slug"] == "mcp-created" for p in listed)


async def test_runs_list_and_show(db, workspace_id, user_id, registry, seeded) -> None:
    run_id = uuid.uuid4()
    async with db() as s:
        run = ExecutionRun(
            id=run_id,
            workspace_id=workspace_id,
            product_id=None,
            status=RunStatus.RUNNING,
            payload={"intent_text": "ship a thing"},
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        s.add(run)
        await s.commit()
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        listed = await registry.call_tool("bsvibe_runs_list", {}, ctx)
        shown = await registry.call_tool("bsvibe_runs_show", {"run_id": str(run_id)}, ctx)
    assert len(listed) == 1
    assert listed[0]["id"] == str(run_id)
    assert listed[0]["intent"] == "ship a thing"
    assert shown["id"] == str(run_id)


async def test_deliverables_list_filters_by_run(
    db, workspace_id, user_id, registry, seeded
) -> None:
    run_id = uuid.uuid4()
    other_run_id = uuid.uuid4()
    async with db() as s:
        for rid in (run_id, other_run_id):
            s.add(
                ExecutionRun(
                    id=rid,
                    workspace_id=workspace_id,
                    product_id=None,
                    status=RunStatus.SHIPPED,
                    payload={},
                    created_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                )
            )
        await s.flush()
        s.add(
            Deliverable(
                workspace_id=workspace_id,
                run_id=run_id,
                deliverable_type=DeliverableType.DIRECT_OUTPUT,
                artifact_uri="s3://x/y",
                payload={},
            )
        )
        s.add(
            Deliverable(
                workspace_id=workspace_id,
                run_id=other_run_id,
                deliverable_type=DeliverableType.DIRECT_OUTPUT,
                artifact_uri="s3://x/z",
                payload={},
            )
        )
        await s.commit()
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        all_d = await registry.call_tool("bsvibe_deliverables_list", {}, ctx)
        filtered = await registry.call_tool(
            "bsvibe_deliverables_list", {"run_id": str(run_id)}, ctx
        )
    assert len(all_d) == 2
    assert len(filtered) == 1
    assert filtered[0]["run_id"] == str(run_id)
