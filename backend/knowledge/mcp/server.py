"""Real MCP protocol server (stdio + Streamable HTTP).

Builds a transport-agnostic ``mcp.server.Server`` instance backed by the
first-class :class:`bsage.mcp.api.ToolRegistry` (Phase 7 / TASK-002).
Three tool surfaces share the registry:

1. **Domain static tools** — :mod:`bsage.mcp.domain_tools` registers the
   nine knowledge tools (``search_knowledge``, ``get_note``,
   ``get_graph_context``, ``list_recent``, ``list_by_tag``,
   ``list_tags``, ``browse_communities``, ``browse_entity``,
   ``create_note``).
2. **Canonicalization tools** — :mod:`bsage.garden.canonicalization
   .mcp_tools` registers the eight read tools always; the four mutation
   tools are gated by ``settings.mcp_canon_mutation_enabled``.
3. **Dynamic plugin tools** — :mod:`bsage.mcp.plugin_bridge` exposes any
   plugin with ``mcp_exposed=True`` directly through plugin loader
   (these stay outside the typed registry because plugins author their
   own JSON Schemas at decorator time).

Old ``_STATIC_TOOL_DEFS`` / ``_STATIC_DISPATCH`` constants are derived
from the registry so the legacy import surface used by existing tests
keeps working.
"""

from __future__ import annotations

import json
from typing import Any

import structlog
from mcp.server import Server
from mcp.types import TextContent, Tool

# TODO(bundle-k-integration): rewire canon MCP tools (originally bsage.garden.canonicalization.mcp_tools; dropped in lift, will land via backend/api in Bundle API)
canon_mcp_tools: Any = None
# TODO(bundle-k-integration): rewire plugin_bridge — Bundle API will inject the
# plugin dispatch surface. Stubbed to None so the module imports cleanly.
plugin_bridge: Any = None
# TODO(bundle-k-integration): wire to dev/transport (out of scope) -- original: from bsage.mcp.admin_tools import register_admin_tools


def get_request_principal() -> Any:
    """Stub — Bundle API will replace with the streamable-HTTP principal extractor."""
    return None


from backend.knowledge.mcp.api import ToolContext, ToolError, ToolRegistry
from backend.knowledge.mcp.domain_tools import register_domain_tools

logger = structlog.get_logger(__name__)

SERVER_NAME = "bsage"


# ---------------------------------------------------------------------------
# Legacy module surface — preserved so existing tests / introspection that
# imports ``_STATIC_TOOL_DEFS`` / ``_STATIC_DISPATCH`` keeps working. The
# actual MCP wire flow goes through the per-build ToolRegistry in
# ``build_server`` — these dicts are derived snapshots, not the source
# of truth.
# ---------------------------------------------------------------------------
def _domain_static_defs() -> list[dict[str, Any]]:
    snapshot_registry = ToolRegistry()
    register_domain_tools(snapshot_registry)
    return [
        {
            "name": t.name,
            "description": t.description,
            "inputSchema": t.inputSchema,
        }
        for t in snapshot_registry.list_tools()
    ]


_STATIC_TOOL_DEFS: list[dict[str, Any]] = _domain_static_defs()


# Legacy ``(state, args) -> dict`` adapters that route through the
# ToolRegistry — preserves the contract used by ``_dispatch_tool`` and
# any external callers that still imported the old ``_STATIC_DISPATCH``
# mapping.
def _legacy_static_dispatch() -> dict[str, Any]:
    snapshot = ToolRegistry()
    register_domain_tools(snapshot)
    if canon_mcp_tools is not None:
        canon_mcp_tools.register_canon_tools(snapshot, mutation_enabled=True)

    def _make(name: str) -> Any:
        async def _call(state: Any, args: dict[str, Any]) -> dict[str, Any]:
            return await snapshot.call_tool(name, args, ToolContext(state=state))

        return _call

    return {name: _make(name) for name in snapshot.names()}


_STATIC_DISPATCH: dict[str, Any] = _legacy_static_dispatch()


# ---------------------------------------------------------------------------
# build_server — primary entry. Constructs a fresh ToolRegistry per call
# (so canon mutation gating can flip per-state) and wires both the
# ``ListTools`` and ``CallTool`` MCP request handlers.
# ---------------------------------------------------------------------------
def build_server(state: Any) -> Server:
    """Construct an MCP Server with all tools registered against ``state``."""
    server: Server = Server(SERVER_NAME)
    registry = _build_registry(state)

    @server.list_tools()  # type: ignore[no-untyped-call,untyped-decorator]
    async def _list_tools() -> list[Tool]:
        tools: list[Tool] = list(registry.list_tools())
        if plugin_bridge is not None:
            plugin_tools = await plugin_bridge.list_plugins_as_tools(state)
            tools.extend(_dict_to_tool(t) for t in plugin_tools)
        return tools

    @server.call_tool()  # type: ignore[untyped-decorator]
    async def _call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
        result = await _dispatch_via_registry(state, registry, name, arguments or {})
        return [TextContent(type="text", text=json.dumps(result, default=str))]

    return server


def _build_registry(state: Any) -> ToolRegistry:
    registry = ToolRegistry()
    register_domain_tools(registry)
    if canon_mcp_tools is not None:
        canon_mcp_tools.register_canon_tools(
            registry,
            mutation_enabled=_canon_mutation_enabled(state),
        )
    # TODO(bundle-k-integration): register_admin_tools (out-of-scope dev/transport)
    # register_admin_tools(registry)
    return registry


def build_registry(state: Any) -> ToolRegistry:
    """Public alias for :func:`_build_registry`.

    Other transports (HTTP ``/mcp/health``, ``bsage mcp list-tools``)
    introspect the same registry the Streamable HTTP server serves —
    exposing the builder as a public symbol keeps that contract explicit.
    """
    return _build_registry(state)


def _canon_mutation_enabled(state: Any) -> bool:
    """Per Handoff §15.2 — MCP approval/mutation tools are off by default.

    Operators opt in by setting ``settings.mcp_canon_mutation_enabled``.
    """
    settings = getattr(state, "settings", None)
    return bool(getattr(settings, "mcp_canon_mutation_enabled", False))


# ---------------------------------------------------------------------------
# Dispatch helpers — first-class registry, then plugin bridge fallback.
# ---------------------------------------------------------------------------
def _authz_context() -> tuple[Any, Any, Any]:
    """Resolve ``(authz_settings, fga, cache)`` for the MCP tool dispatcher.

    Tier 5 Phase 3a — the dispatcher's ``required_permission`` check runs
    through ``bsvibe_authz.check_tenant_permission``, which needs the
    ``bsvibe_authz.Settings`` (``openfga_api_url`` decides permissive
    mode), the OpenFGA client, and the permission cache. We resolve the
    process-wide singletons the REST ``require_permission`` dependency
    uses so MCP and REST share one OpenFGA client + one 30s cache.
    """
    from backend.shared.authz import get_openfga_client, get_permission_cache, get_settings

    authz_settings = get_settings()
    return (
        authz_settings,
        get_openfga_client(authz_settings),
        get_permission_cache(authz_settings),
    )


def _resolve_principal(state: Any) -> Any | None:
    """Return the principal for the current MCP call.

    The Streamable HTTP transport resolves the principal from the
    request's ``Authorization`` header and stashes it on a context-var;
    we read it back here so ``ToolContext.user`` is the real principal.
    Outside an HTTP request (stdio transport, tests) the context-var is
    unset and we fall back to ``state.mcp_principal`` if a caller set
    one, else ``None``.
    """
    # TODO(bundle-k-integration): wire to dev/transport (out of scope) -- original: from bsage.mcp.streamable_http import get_request_principal

    principal = get_request_principal()
    if principal is not None:
        return principal
    return getattr(state, "mcp_principal", None)


async def _dispatch_via_registry(
    state: Any,
    registry: ToolRegistry,
    name: str,
    arguments: dict[str, Any],
) -> Any:
    if name in registry:
        authz_settings, fga, cache = _authz_context()
        # The Streamable HTTP transport (bsage.mcp.streamable_http) carries
        # the ``Authorization`` header on every request, resolves the
        # principal per-request, and stashes it on a context-var that
        # ``_resolve_principal`` reads here — so ``ctx.user`` is the real
        # principal and permissioned tools authorize correctly over HTTP.
        # The stdio transport has no per-request HTTP headers; it pins
        # the principal on ``state.mcp_principal`` at startup from
        # ``$BSAGE_MCP_PAT`` (see ``bsage.mcp.stdio``) and
        # ``_resolve_principal`` reads it back here. When the PAT is
        # unset the principal is ``None`` — domain read tools
        # (``required_permission=None``) still work; permissioned tools deny.
        ctx = ToolContext(
            state=state,
            user=_resolve_principal(state),
            settings=authz_settings,
            fga=fga,
            cache=cache,
            audit_outbox=getattr(state, "audit_outbox", None),
        )
        try:
            return await registry.call_tool(name, arguments, ctx)
        except ToolError:
            # ToolError is wire-safe — surface its message verbatim so
            # the MCP framework's call_tool wrapper can render it as
            # error content.
            raise
    if plugin_bridge is None:
        raise ToolError(f"tool {name!r} not registered and plugin_bridge unwired")
    return await plugin_bridge.invoke_plugin_as_tool(state, name, arguments)


async def _dispatch_tool(state: Any, name: str, arguments: dict[str, Any]) -> Any:
    """Legacy module-level dispatcher. Tests pin this signature.

    Builds a per-call registry (cheap — just dataclass construction) so
    the test contract (canon optional gated by
    ``settings.mcp_canon_mutation_enabled``, plugin fallback via
    ``plugin_bridge``) holds without mutating module state.
    """
    registry = _build_registry(state)
    return await _dispatch_via_registry(state, registry, name, arguments)


def _dict_to_tool(d: dict[str, Any]) -> Tool:
    """Convert plugin-bridge ``{name, description, inputSchema}`` dict into mcp.Tool."""
    return Tool(
        name=d["name"],
        description=d.get("description", ""),
        inputSchema=d.get("inputSchema", {"type": "object"}),
    )
