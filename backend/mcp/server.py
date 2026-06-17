"""MCP server bootstrap — wires the ToolRegistry to the MCP SDK Server.

The :class:`mcp.server.Server` is transport-agnostic; this module wires
its two request handlers (``ListTools`` / ``CallTool``) to the in-process
:class:`ToolRegistry`, builds a :class:`ToolContext` per call from the
contextvar-stashed principal + the request-scoped DB session, and
dispatches to the right typed handler.
"""

from __future__ import annotations

import json
from typing import Any

import structlog
from mcp.server import Server
from mcp.types import TextContent
from mcp.types import Tool as McpTool
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.mcp.api import McpPrincipal, ToolContext, ToolError, ToolRegistry, ToolScopeDenied
from backend.mcp.principal import get_request_principal
from backend.mcp.tools import register_all_tools

logger = structlog.get_logger(__name__)

SERVER_NAME = "bsvibe"


def build_registry() -> ToolRegistry:
    """Build a fresh :class:`ToolRegistry` with every D2 tool registered."""
    registry = ToolRegistry()
    register_all_tools(registry)
    return registry


def build_server(
    *,
    session_factory: async_sessionmaker[Any],
    registry: ToolRegistry | None = None,
    delivery_dispatcher: Any | None = None,
) -> Server:
    """Construct an MCP :class:`Server` wired to ``session_factory``.

    ``session_factory`` is the same factory the FastAPI app uses for
    REST handlers — every tool call opens one session, scopes it to
    the principal's workspace via :class:`ToolContext`, and tears it
    down at the end of the call.

    ``delivery_dispatcher`` (Lift E40) — when provided, every
    :class:`ToolContext` exposes it under
    ``extras["delivery_dispatcher"]`` so the safe-mode-approve handler
    can dispatch the outbound delivery through the same code path the
    REST route uses. Built by the FastAPI app (which has the wider
    import surface) and passed in to keep the MCP context's
    import-contract intact ([[bsvibe-mcp-ui-parity]]).
    """
    server: Server = Server(SERVER_NAME)
    reg = registry or build_registry()

    @server.list_tools()  # type: ignore[no-untyped-call,untyped-decorator]
    async def _list_tools() -> list[McpTool]:
        return list(reg.list_tools())

    @server.call_tool()  # type: ignore[untyped-decorator]
    async def _call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
        principal = get_request_principal()
        if principal is None:
            # Should not happen in production — the transport returns 401
            # before the dispatcher ever runs. Belt-and-braces guard.
            raise ToolError("unauthenticated")
        async with session_factory() as session:
            extras: dict[str, Any] = {}
            if delivery_dispatcher is not None:
                extras["delivery_dispatcher"] = delivery_dispatcher
            ctx = ToolContext(
                principal=principal,
                session=session,
                session_factory=session_factory,
                extras=extras,
            )
            try:
                result = await reg.call_tool(name, arguments or {}, ctx)
            except ToolScopeDenied:
                raise
            except ToolError:
                raise
        return [TextContent(type="text", text=json.dumps(result, default=str))]

    return server


__all__ = [
    "SERVER_NAME",
    "build_registry",
    "build_server",
    "McpPrincipal",
]
