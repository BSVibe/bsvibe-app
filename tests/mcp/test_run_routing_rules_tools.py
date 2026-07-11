"""Run-routing rule tool handler tests — Lift E7.

These cover the new ``bsvibe_run_routing_rules_*`` MCP surface that
mirrors ``/api/v1/run-routing``. Distinct from the legacy
``bsvibe_routing_rules_*`` tools, which sit on the model-routing rules
fed to the litellm hook.
"""

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
import backend.router.routing.run_routing.db  # noqa: F401
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
    """Seed the workspace BEFORE child rows so PG FK references resolve."""
    async with db() as s:
        s.add(WorkspaceRow(id=workspace_id, name="ws", region="us-1"))
        await s.commit()
    yield


async def test_create_lists_delete_round_trip(db, workspace_id, user_id, registry, seeded) -> None:
    # Create — non-default rule with caller_id (top-level column).
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
            "bsvibe_run_routing_rules_create",
            {
                "name": "design-to-codex",
                "caller_id": "workflow.agent_loop.plan",
                "priority": 10,
                "is_default": False,
                "target": "executor/codex",
            },
            ctx,
        )
    assert created["name"] == "design-to-codex"
    assert created["caller_id"] == "workflow.agent_loop.plan"
    assert created["target"] == "executor/codex"
    assert created["is_default"] is False
    assert created["is_active"] is True
    rule_id = created["id"]

    # List
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        listed = await registry.call_tool("bsvibe_run_routing_rules_list", {}, ctx)
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
        out = await registry.call_tool("bsvibe_run_routing_rules_delete", {"rule_id": rule_id}, ctx)
    assert out["deleted"] is True


async def test_create_default_rule_without_caller_id(
    db, workspace_id, user_id, registry, seeded
) -> None:
    """A default (catch-all) rule may omit caller_id."""
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
            "bsvibe_run_routing_rules_create",
            {
                "name": "default-catch-all",
                "priority": 100,
                "is_default": True,
                "target": "ollama_chat/qwen3",
            },
            ctx,
        )
    assert created["is_default"] is True
    assert created["caller_id"] is None


async def test_create_with_caller_id_condition(db, workspace_id, user_id, registry, seeded) -> None:
    """Back-compat: non-default rule may declare caller via a condition clause."""
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
            "bsvibe_run_routing_rules_create",
            {
                "name": "impl-condition-form",
                "priority": 5,
                "is_default": False,
                "target": "executor/opencode",
                "conditions": [
                    {
                        "field": "caller_id",
                        "operator": "eq",
                        "value": "impl",
                    }
                ],
            },
            ctx,
        )
    assert len(created["conditions"]) == 1
    assert created["conditions"][0]["field"] == "caller_id"


async def test_create_rejects_unknown_caller_id(
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
                "bsvibe_run_routing_rules_create",
                {
                    "name": "bad-caller",
                    "caller_id": "not_a_real_caller",
                    "priority": 1,
                    "target": "executor/codex",
                },
                ctx,
            )


async def test_create_accepts_skill_namespace_caller(
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
        created = await registry.call_tool(
            "bsvibe_run_routing_rules_create",
            {
                "name": "review-skill-route",
                "caller_id": "skill.code_review",
                "priority": 3,
                "is_default": False,
                "target": "executor/opencode",
            },
            ctx,
        )
    assert created["caller_id"] == "skill.code_review"


async def test_non_default_requires_caller_id(db, workspace_id, user_id, registry, seeded) -> None:
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
                "bsvibe_run_routing_rules_create",
                {
                    "name": "no-caller",
                    "priority": 1,
                    "is_default": False,
                    "target": "executor/codex",
                },
                ctx,
            )


async def test_create_rejects_unknown_field_in_conditions(
    db, workspace_id, user_id, registry, seeded
) -> None:
    """Conditions are validated against the engine ALLOWED_FIELDS. Legacy
    heuristic vocab NOT absorbed into the unified table (e.g. ``user_text``)
    is still rejected — only the Lift-1 content signals crossed over."""
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
                "bsvibe_run_routing_rules_create",
                {
                    "name": "bad-field",
                    "caller_id": "workflow.agent_loop.plan",
                    "priority": 1,
                    "is_default": False,
                    "target": "executor/codex",
                    "conditions": [{"field": "user_text", "operator": "contains", "value": "x"}],
                },
                ctx,
            )


async def test_create_accepts_absorbed_content_signal_field(
    db, workspace_id, user_id, registry, seeded
) -> None:
    """Lift 1: the content signals absorbed from the deleted Layer-2 engine
    (``estimated_tokens`` / ``classified_intent`` / ``detected_language``)
    are now valid condition fields on the unified run-routing table."""
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(
                workspace_id=workspace_id,
                user_id=user_id,
                scopes=("mcp:read", "mcp:write"),
            ),
            session=s,
        )
        result = await registry.call_tool(
            "bsvibe_run_routing_rules_create",
            {
                "name": "big-context-to-opus",
                "caller_id": "workflow.agent_loop.plan",
                "priority": 1,
                "is_default": False,
                "target": "executor/codex",
                "conditions": [{"field": "estimated_tokens", "operator": "gt", "value": 1000}],
            },
            ctx,
        )
        assert result.get("id")


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
            "bsvibe_run_routing_rules_create",
            {
                "name": "dup",
                "caller_id": "workflow.agent_loop.plan",
                "priority": 1,
                "target": "executor/codex",
            },
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
                "bsvibe_run_routing_rules_create",
                {
                    "name": "dup",
                    "caller_id": "workflow.agent_loop.act",
                    "priority": 2,
                    "target": "executor/opencode",
                },
                ctx,
            )


async def test_create_requires_write_scope(db, workspace_id, user_id, registry, seeded) -> None:
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        with pytest.raises(Exception, match="requires scope"):
            await registry.call_tool(
                "bsvibe_run_routing_rules_create",
                {
                    "name": "x",
                    "caller_id": "workflow.agent_loop.plan",
                    "priority": 1,
                    "target": "executor/codex",
                },
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
            "bsvibe_run_routing_rules_create",
            {
                "name": "mine",
                "caller_id": "workflow.agent_loop.plan",
                "priority": 1,
                "target": "executor/codex",
            },
            ctx,
        )

    # Create one in other workspace
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
            "bsvibe_run_routing_rules_create",
            {
                "name": "theirs",
                "caller_id": "workflow.agent_loop.act",
                "priority": 1,
                "target": "executor/opencode",
            },
            ctx_other,
        )

    # List as caller
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        listed = await registry.call_tool("bsvibe_run_routing_rules_list", {}, ctx)
    names = {r["name"] for r in listed}
    assert names == {"mine"}


async def test_list_returns_priority_ascending(db, workspace_id, user_id, registry, seeded) -> None:
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(
                workspace_id=workspace_id,
                user_id=user_id,
                scopes=("mcp:read", "mcp:write"),
            ),
            session=s,
        )
        for name, caller, prio in (
            ("rule-high-prio", "workflow.agent_loop.plan", 50),
            ("rule-low-prio", "workflow.agent_loop.act", 5),
            ("rule-mid-prio", "skill.review", 25),
        ):
            await registry.call_tool(
                "bsvibe_run_routing_rules_create",
                {
                    "name": name,
                    "caller_id": caller,
                    "priority": prio,
                    "target": "executor/codex",
                },
                ctx,
            )
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read",)),
            session=s,
        )
        listed = await registry.call_tool("bsvibe_run_routing_rules_list", {}, ctx)
    priorities = [r["priority"] for r in listed]
    assert priorities == sorted(priorities)


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
                "bsvibe_run_routing_rules_delete", {"rule_id": str(uuid.uuid4())}, ctx
            )
