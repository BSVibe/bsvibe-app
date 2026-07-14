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
from backend.mcp.tools.work_tools import is_work_tool

logger = structlog.get_logger(__name__)

SERVER_NAME = "bsvibe"


def build_registry(
    *,
    record_question: Any | None = None,
    record_deliverable: Any | None = None,
) -> ToolRegistry:
    """Build a fresh :class:`ToolRegistry` with every D2 tool registered.

    ``record_question`` / ``record_deliverable`` (T1b) are the two loop-owned effects behind
    the run-scoped work tools, injected from the composition root — see
    :func:`backend.mcp.tools.register_all_tools`."""
    registry = ToolRegistry()
    register_all_tools(
        registry, record_question=record_question, record_deliverable=record_deliverable
    )
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
        # A RUN-SCOPED principal is a dispatched executor task: it may act on its run and
        # nothing else. Offering it the whole surface was two bugs in one (measured against
        # prod, 2026-07-14): the task token could call `products_list` / `safe_mode_set` /
        # `workers_revoke`, and the CLI — handed all 86 tools while sanctioned for 9 — failed
        # the worker's own `system/init` check, so no agentic run could start.
        principal = get_request_principal()
        tools = list(reg.list_tools())
        if principal is not None and principal.run_id is not None:
            return [t for t in tools if is_work_tool(t.name)]
        return tools

    @server.call_tool()  # type: ignore[untyped-decorator]
    async def _call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
        principal = get_request_principal()
        if principal is None:
            # Should not happen in production — the transport returns 401
            # before the dispatcher ever runs. Belt-and-braces guard.
            raise ToolError("unauthenticated")
        if principal.run_id is not None and not is_work_tool(name):
            # Hiding a tool from tools/list is cosmetic — the call is what has to be refused.
            raise ToolScopeDenied(
                f"{name} is not a work tool: a run-scoped task token may act only on its run"
            )
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
