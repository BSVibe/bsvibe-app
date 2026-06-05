"""Routing rule tool handler tests — Lift D3b."""

from __future__ import annotations

import base64
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

# Imported for table registration on the shared Base.metadata.
import backend.identity.db  # noqa: F401
import backend.identity.workspaces_db  # noqa: F401
import backend.router.accounts.account_models  # noqa: F401
import backend.router.accounts.models  # noqa: F401
import backend.router.rules.db  # noqa: F401
from backend.config import get_settings
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
    """Seed the workspace BEFORE child rows so PG FK references resolve
    (sqlalchemy-test-fixture-pg-fk-insert-order skill)."""
    async with db() as s:
        s.add(WorkspaceRow(id=workspace_id, name="ws", region="us-1"))
        await s.commit()
    yield


async def test_create_lists_delete_round_trip(db, workspace_id, user_id, registry, seeded) -> None:
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
            "bsvibe_routing_rules_create",
            {
                "name": "chore-to-local",
                "target_model": "ollama_chat/qwen3-coder:30b",
                "priority": 10,
                "is_default": False,
            },
            ctx,
        )
    assert created["name"] == "chore-to-local"
    assert created["target_model"] == "ollama_chat/qwen3-coder:30b"
    rule_id = created["id"]

    # List
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        listed = await registry.call_tool("bsvibe_routing_rules_list", {}, ctx)
    assert isinstance(listed, list)
    assert any(r["id"] == rule_id for r in listed)

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
        out = await registry.call_tool("bsvibe_routing_rules_delete", {"rule_id": rule_id}, ctx)
    assert out["deleted"] is True


async def test_create_with_conditions(db, workspace_id, user_id, registry, seeded) -> None:
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
            "bsvibe_routing_rules_create",
            {
                "name": "deep-to-opencode",
                "target_model": "opencode/sonnet-4",
                "priority": 5,
                "conditions": [
                    {
                        "condition_type": "text",
                        "field": "user_text",
                        "operator": "contains",
                        "value": "deep",
                    }
                ],
            },
            ctx,
        )
    assert len(created["conditions"]) == 1
    assert created["conditions"][0]["field"] == "user_text"


async def test_create_requires_write_scope(db, workspace_id, user_id, registry, seeded) -> None:
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        with pytest.raises(Exception, match="requires scope"):
            await registry.call_tool(
                "bsvibe_routing_rules_create",
                {
                    "name": "x",
                    "target_model": "ollama_chat/foo",
                    "priority": 1,
                },
                ctx,
            )


async def test_create_rejects_unknown_condition_field(
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
        with pytest.raises(ToolError, match="invalid arguments"):
            await registry.call_tool(
                "bsvibe_routing_rules_create",
                {
                    "name": "bad-cond",
                    "target_model": "ollama_chat/foo",
                    "priority": 2,
                    "conditions": [
                        {
                            "condition_type": "text",
                            "field": "not_a_real_field",
                            "operator": "eq",
                            "value": "x",
                        }
                    ],
                },
                ctx,
            )


async def test_create_rejects_duplicate_name(db, workspace_id, user_id, registry, seeded) -> None:
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(
                workspace_id=workspace_id,
                user_id=user_id,
                scopes=("mcp:read", "mcp:write"),
            ),
            session=s,
        )
        await registry.call_tool(
            "bsvibe_routing_rules_create",
            {"name": "dup", "target_model": "ollama_chat/a", "priority": 1},
            ctx,
        )
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(
                workspace_id=workspace_id,
                user_id=user_id,
                scopes=("mcp:read", "mcp:write"),
            ),
            session=s,
        )
        with pytest.raises(ToolError, match="already exists"):
            await registry.call_tool(
                "bsvibe_routing_rules_create",
                {"name": "dup", "target_model": "ollama_chat/b", "priority": 2},
                ctx,
            )


async def test_list_workspace_scoped(db, workspace_id, user_id, registry, seeded) -> None:
    """A rule in another workspace must not leak into this principal's list."""
    other_ws = uuid.uuid4()
    async with db() as s:
        s.add(WorkspaceRow(id=other_ws, name="other", region="us-1"))
        await s.commit()

    # Create a rule in caller workspace
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(
                workspace_id=workspace_id,
                user_id=user_id,
                scopes=("mcp:read", "mcp:write"),
            ),
            session=s,
        )
        await registry.call_tool(
            "bsvibe_routing_rules_create",
            {"name": "mine", "target_model": "ollama_chat/a", "priority": 1},
            ctx,
        )

    # Create one in other workspace via the same MCP tool (different principal)
    async with db() as s:
        ctx_other = ToolContext(
            principal=_principal(
                workspace_id=other_ws,
                user_id=user_id,
                scopes=("mcp:read", "mcp:write"),
            ),
            session=s,
        )
        await registry.call_tool(
            "bsvibe_routing_rules_create",
            {"name": "theirs", "target_model": "ollama_chat/b", "priority": 1},
            ctx_other,
        )

    # List as caller
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        listed = await registry.call_tool("bsvibe_routing_rules_list", {}, ctx)
    names = {r["name"] for r in listed}
    assert names == {"mine"}


async def test_delete_returns_error_when_not_found(
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
        with pytest.raises(ToolError, match="not found"):
            await registry.call_tool(
                "bsvibe_routing_rules_delete", {"rule_id": str(uuid.uuid4())}, ctx
            )
