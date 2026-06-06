"""Model account tool handler tests — Lift D3a."""

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
    async with db() as s:
        s.add(WorkspaceRow(id=workspace_id, name="ws", region="us-1"))
        await s.commit()
    yield


async def test_create_lists_show_delete_round_trip(
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
            "bsvibe_model_accounts_create",
            {
                "provider": "anthropic",
                "label": "primary",
                "litellm_model": "claude-opus-4-7",
                "api_key": "sk-test-XXXX",
            },
            ctx,
        )
    assert created["provider"] == "anthropic"
    assert created["label"] == "primary"
    assert created["has_api_key"] is True
    assert "api_key" not in created  # redacted
    new_id = created["id"]

    # List
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        listed = await registry.call_tool("bsvibe_model_accounts_list", {}, ctx)
    assert isinstance(listed, list)
    assert any(r["id"] == new_id for r in listed)
    assert all("api_key" not in r for r in listed)

    # Show
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        shown = await registry.call_tool(
            "bsvibe_model_accounts_show", {"model_account_id": new_id}, ctx
        )
    assert shown["id"] == new_id
    assert shown["provider"] == "anthropic"

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
        out = await registry.call_tool(
            "bsvibe_model_accounts_delete", {"model_account_id": new_id}, ctx
        )
    assert out["deleted"] is True
    # Re-show: not found
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        with pytest.raises(ToolError, match="model account not found"):
            await registry.call_tool(
                "bsvibe_model_accounts_show", {"model_account_id": new_id}, ctx
            )


async def test_create_requires_write_scope(db, workspace_id, user_id, registry, seeded) -> None:
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        with pytest.raises(Exception, match="requires scope"):
            await registry.call_tool(
                "bsvibe_model_accounts_create",
                {
                    "provider": "openai",
                    "label": "x",
                    "litellm_model": "gpt-4o",
                    "api_key": "sk-x",
                },
                ctx,
            )


async def test_delete_requires_write_scope(db, workspace_id, user_id, registry, seeded) -> None:
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        with pytest.raises(Exception, match="requires scope"):
            await registry.call_tool(
                "bsvibe_model_accounts_delete",
                {"model_account_id": str(uuid.uuid4())},
                ctx,
            )


async def test_list_scoped_to_workspace(db, workspace_id, user_id, registry, seeded) -> None:
    other_ws = uuid.uuid4()
    async with db() as s:
        s.add(WorkspaceRow(id=other_ws, name="other", region="us-1"))
        await s.commit()
    # Create one in mine, one in other.
    async with db() as s:
        ctx_mine = ToolContext(
            principal=_principal(
                workspace_id=workspace_id,
                user_id=user_id,
                scopes=("mcp:read", "mcp:write"),
            ),
            session=s,
        )
        await registry.call_tool(
            "bsvibe_model_accounts_create",
            {
                "provider": "anthropic",
                "label": "mine",
                "litellm_model": "claude",
                "api_key": "sk-m",
            },
            ctx_mine,
        )
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
            "bsvibe_model_accounts_create",
            {
                "provider": "openai",
                "label": "other",
                "litellm_model": "gpt-4o",
                "api_key": "sk-o",
            },
            ctx_other,
        )

    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        listed = await registry.call_tool("bsvibe_model_accounts_list", {}, ctx)
    labels = {r["label"] for r in listed}
    assert labels == {"mine"}
