"""MCP server bootstrap — wires the ToolRegistry to the MCP SDK Server.

The :class:`mcp.server.Server` is transport-agnostic; this module wires
its two request handlers (``ListTools`` / ``CallTool``) to the in-process
:class:`ToolRegistry`, builds a :class:`ToolContext` per call from the
contextvar-stashed principal + the request-scoped DB session, and
dispatches to the right typed handler.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable
from typing import Any, Protocol

import structlog
from mcp.server import Server
from mcp.types import TextContent
from mcp.types import Tool as McpTool
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.data.rls import set_workspace_guc
from backend.data.scoping import reset_current_workspace_id, set_current_workspace_id
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
    record_progress: Any | None = None,
) -> ToolRegistry:
    """Build a fresh :class:`ToolRegistry` with every D2 tool registered.

    ``record_question`` / ``record_deliverable`` (T1b) are the two loop-owned effects behind
    the run-scoped work tools, injected from the composition root — see
    :func:`backend.mcp.tools.register_all_tools`."""
    registry = ToolRegistry()
    register_all_tools(
        registry,
        record_question=record_question,
        record_deliverable=record_deliverable,
        record_progress=record_progress,
    )
    return registry


class HomeRoutingPort(Protocol):
    """Where runs live (:class:`backend.mcp.home_forwarding.HomeRouting`)."""

    @property
    def machine(self) -> str: ...

    async def locate(self, session: Any, run_id: Any) -> str | None: ...

    def forward(
        self, home: str, principal: McpPrincipal, name: str, arguments: dict[str, Any]
    ) -> Awaitable[str]: ...


class ToolExecutor(Protocol):
    """Runs one authorised tool call and returns its result as wire text."""

    def __call__(
        self,
        principal: McpPrincipal,
        name: str,
        arguments: dict[str, Any],
        *,
        forwarded: bool = False,
    ) -> Awaitable[str]: ...


def build_tool_executor(
    *,
    session_factory: async_sessionmaker[Any],
    registry: ToolRegistry,
    delivery_dispatcher: Any | None = None,
    client_sandbox: Any | None = None,
    declared_secrets: Any | None = None,
    retract_handler: Any | None = None,
    narrative_generator: Any | None = None,
    home_routing: HomeRoutingPort | None = None,
) -> ToolExecutor:
    """The one path a tool call takes once its caller is known — in place, or at its run's home.

    ``home_routing`` (#929, B): a run-scoped call for a run whose ``home_machine`` is ANOTHER
    machine is answered there, because only that machine holds the run's worktree and sandbox.
    ``forwarded`` marks a call that already arrived that way: it runs here, never bounces on.
    ``None`` (a single box) runs everything in place.
    """
    reg = registry

    async def _home_elsewhere(principal: McpPrincipal) -> str | None:
        assert home_routing is not None
        # A short session of its own: the forward may wait minutes, and must not pin a
        # pooled connection while it does.
        async with session_factory() as session:
            ws_token = set_current_workspace_id(principal.workspace_id)
            try:
                await set_workspace_guc(await session.connection(), principal.workspace_id)
                home = await home_routing.locate(session, principal.run_id)
            finally:
                reset_current_workspace_id(ws_token)
        return home if home and home != home_routing.machine else None

    async def _execute(
        principal: McpPrincipal,
        name: str,
        arguments: dict[str, Any],
        *,
        forwarded: bool = False,
    ) -> str:
        # Here, not in the transport handler: a call that arrived by forwarding is checked
        # again at home.
        if principal.run_id is not None and not is_work_tool(name):
            # Hiding a tool from tools/list is cosmetic — the call is what has to be refused.
            raise ToolScopeDenied(
                f"{name} is not a work tool: a run-scoped task token may act only on its run"
            )
        if home_routing is not None and principal.run_id is not None and not forwarded:
            home = await _home_elsewhere(principal)
            if home is not None:
                return await home_routing.forward(home, principal, name, arguments)
        async with session_factory() as session:
            # Tenant-isolation defense-in-depth — mirror REST get_workspace_id so
            # the agent surface is NOT on single-layer per-handler scoping:
            #   layer 2 — the ORM auto-filter contextvar (backend.data.scoping)
            #   layer 3 — the Postgres RLS GUC (fail-closed when unset → no rows)
            # Without this a future MCP tool that forgets its workspace filter is a
            # silent cross-tenant read/write with no net.
            ws_token = set_current_workspace_id(principal.workspace_id)
            conn = await session.connection()
            await set_workspace_guc(conn, principal.workspace_id)
            extras: dict[str, Any] = {}
            if delivery_dispatcher is not None:
                extras["delivery_dispatcher"] = delivery_dispatcher
            if client_sandbox is not None:
                # Which BOX does this run act in? A client_attach run acts on the
                # founder's own machine, and addressing it means reaching the executor
                # dispatch substrate — forbidden to this context, so it is handed in.
                extras["client_sandbox"] = client_sandbox
            if declared_secrets is not None:
                # #691 — the product's declared secrets for its server box. Unsealing
                # reaches the credential cipher, forbidden to this context: handed in.
                extras["declared_secrets"] = declared_secrets
            if narrative_generator is not None:
                # Writes the report's plain-language sentence. Same reason again:
                # it calls an LLM through the workspace's model account. Absent,
                # the report still composes — it just has no fresh narrative.
                extras["narrative_generator"] = narrative_generator
            if retract_handler is not None:
                # The runtime that calls a plugin's @p.compensate. Same reason as
                # the two above: it reaches backend.extensions / .connectors /
                # .router. The RULE it serves lives in workflow.application, so
                # both surfaces run one retraction, not two.
                extras["retract_handler"] = retract_handler
            ctx = ToolContext(
                principal=principal,
                session=session,
                session_factory=session_factory,
                extras=extras,
            )
            try:
                result = await reg.call_tool(name, arguments, ctx)
            finally:
                reset_current_workspace_id(ws_token)
        return json.dumps(result, default=str)

    return _execute


def build_server(
    *,
    session_factory: async_sessionmaker[Any],
    registry: ToolRegistry | None = None,
    delivery_dispatcher: Any | None = None,
    client_sandbox: Any | None = None,
    declared_secrets: Any | None = None,
    retract_handler: Any | None = None,
    narrative_generator: Any | None = None,
    home_routing: HomeRoutingPort | None = None,
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
    execute = build_tool_executor(
        session_factory=session_factory,
        registry=reg,
        delivery_dispatcher=delivery_dispatcher,
        client_sandbox=client_sandbox,
        declared_secrets=declared_secrets,
        retract_handler=retract_handler,
        narrative_generator=narrative_generator,
        home_routing=home_routing,
    )

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
        return [TextContent(type="text", text=await execute(principal, name, arguments or {}))]

    return server


__all__ = [
    "SERVER_NAME",
    "build_registry",
    "build_server",
    "build_tool_executor",
    "McpPrincipal",
]
