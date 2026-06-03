"""ToolRegistry dispatcher unit tests — input/scope/output enforcement."""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import MagicMock

import pytest
from pydantic import BaseModel, ConfigDict

from backend.mcp.api import (
    McpPrincipal,
    Tool,
    ToolContext,
    ToolError,
    ToolRegistry,
    ToolScopeDenied,
)

pytestmark = pytest.mark.asyncio


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: int


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid")
    doubled: int


async def _handler_ok(args: _In, ctx: ToolContext) -> Any:
    return {"doubled": args.value * 2}


def _principal(scopes: tuple[str, ...] = ("mcp:read",)) -> McpPrincipal:
    return McpPrincipal(
        user_id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        client_id="dcr-test",
        scopes=frozenset(scopes),
        jti=uuid.uuid4(),
    )


def _ctx(scopes: tuple[str, ...] = ("mcp:read",)) -> ToolContext:
    return ToolContext(principal=_principal(scopes), session=MagicMock())


def _register(scopes: tuple[str, ...] = ()) -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(
        Tool(
            name="t_double",
            description="double the input",
            input_schema=_In,
            output_schema=_Out,
            handler=_handler_ok,
            required_scopes=scopes,
        )
    )
    return reg


async def test_registry_dispatch_validates_input_against_schema() -> None:
    reg = _register()
    with pytest.raises(ToolError, match="invalid arguments"):
        await reg.call_tool("t_double", {"value": "not-an-int"}, _ctx())


async def test_registry_dispatch_validates_output_against_schema() -> None:
    async def bad_handler(args: _In, ctx: ToolContext) -> Any:
        return {"wrong_field": 1}

    reg = ToolRegistry()
    reg.register(
        Tool(
            name="t_bad",
            description="",
            input_schema=_In,
            output_schema=_Out,
            handler=bad_handler,
        )
    )
    with pytest.raises(ToolError, match="invalid output"):
        await reg.call_tool("t_bad", {"value": 1}, _ctx())


async def test_registry_dispatch_enforces_required_scope() -> None:
    reg = _register(scopes=("mcp:write",))
    with pytest.raises(ToolScopeDenied):
        await reg.call_tool("t_double", {"value": 1}, _ctx(scopes=("mcp:read",)))


async def test_registry_dispatch_passes_when_scope_present() -> None:
    reg = _register(scopes=("mcp:write",))
    result = await reg.call_tool("t_double", {"value": 3}, _ctx(scopes=("mcp:read", "mcp:write")))
    assert result == {"doubled": 6}


async def test_registry_dispatch_handler_exception_translates_to_tool_error() -> None:
    async def boom(args: _In, ctx: ToolContext) -> Any:
        raise RuntimeError("internal detail that must NOT leak")

    reg = ToolRegistry()
    reg.register(
        Tool(
            name="t_boom",
            description="",
            input_schema=_In,
            output_schema=_Out,
            handler=boom,
        )
    )
    with pytest.raises(ToolError, match="failed: RuntimeError"):
        await reg.call_tool("t_boom", {"value": 1}, _ctx())


async def test_registry_unknown_tool_raises() -> None:
    with pytest.raises(ToolError, match="unknown tool"):
        await ToolRegistry().call_tool("nope", {}, _ctx())


async def test_registry_register_rejects_duplicate_name() -> None:
    reg = _register()
    with pytest.raises(ValueError, match="already registered"):
        reg.register(
            Tool(
                name="t_double",
                description="dup",
                input_schema=_In,
                output_schema=_Out,
                handler=_handler_ok,
            )
        )


async def test_list_tools_returns_one_mcp_tool_per_registration() -> None:
    # async marker just so pytest-asyncio's auto-mode doesn't warn about an
    # asyncio-marked sync test (this file applies ``pytestmark = pytest.mark.asyncio``).
    reg = _register()
    tools = reg.list_tools()
    assert len(tools) == 1
    assert tools[0].name == "t_double"
    assert isinstance(tools[0].inputSchema, dict)
    assert "properties" in tools[0].inputSchema
