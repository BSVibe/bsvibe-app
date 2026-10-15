"""MCP parity: ``bsvibe_model_accounts_create`` refuses an inward endpoint as REST does."""

from __future__ import annotations

import pytest

from backend.mcp.api import ToolContext, ToolError

from .test_model_accounts_tools import (  # noqa: F401 — fixtures
    _principal,
    db,
    registry,
    seeded,
    user_id,  # noqa: F811
    workspace_id,  # noqa: F811
)

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("dispatch_publishes_workspace")]


async def test_mcp_create_aimed_inward_is_a_tool_error(
    db,  # noqa: F811
    workspace_id,  # noqa: F811
    user_id,  # noqa: F811
    registry,  # noqa: F811
    seeded,  # noqa: F811
) -> None:
    async with db() as s:
        ctx = ToolContext(
            principal=_principal(
                workspace_id=workspace_id, user_id=user_id, scopes=("mcp:read", "mcp:write")
            ),
            session=s,
        )
        with pytest.raises(ToolError, match="public"):
            await registry.call_tool(
                "bsvibe_model_accounts_create",
                {
                    "provider": "openai",
                    "label": "x",
                    "litellm_model": "openai/gpt-4o",
                    "api_key": "k",
                    "api_base": "http://169.254.169.254",
                },
                ctx,
            )
