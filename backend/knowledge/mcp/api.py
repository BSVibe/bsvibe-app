"""First-class MCP API primitives — Phase 7 / TASK-002.

This module elevates MCP tools to a first-class API surface alongside
the REST routers in :mod:`bsage.gateway.routes`. Each tool ships with:

* a typed Pydantic ``input_schema`` (drives ListTools' JSON Schema)
* a typed Pydantic ``output_schema`` (validates handler return values)
* an async ``handler`` that talks to the same service layer the REST
  routes use — never the CLI / typer command function
* an optional ``required_permission`` — a ``<product>.<resource>.<action>``
  dot-string checked against OpenFGA via
  ``bsvibe_authz.check_tenant_permission``. Tier 5 Phase 3a unifies MCP
  tool authorization with REST ``require_permission``: one OpenFGA model
  gates both surfaces. (Replaces the legacy ``required_scopes`` list,
  which checked the JWT ``scope`` claim — a separate authz path.)
* an optional ``audit_event`` — emitted on success via the same
  audit outbox the REST routes use, so every mutating tool is
  observable identically to its REST sibling.

The dispatcher (``ToolRegistry``) deliberately mirrors how FastAPI
routers behave: validate input → enforce permission → run handler →
validate output → audit emit on success.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

import structlog
from mcp.types import Tool as McpTool
from pydantic import BaseModel, ValidationError

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
class ToolError(Exception):
    """Generic dispatcher error.

    Wire-safe — must never carry implementation details from the
    underlying handler. The dispatcher catches every internal exception
    and re-raises a ``ToolError`` with a sanitised message.
    """


class ToolScopeDenied(ToolError):  # noqa: N818 — wire-stable public API name
    """Raised when the principal is denied a tool's ``required_permission``.

    Name is kept for wire/import stability — Tier 5 Phase 3a moved the
    underlying check from JWT scope claims to an OpenFGA permission check.
    """


# Backwards-compatible alias — Tier 5 Phase 3a renamed the concept from
# "scope" to "permission"; new code may import either name.
ToolPermissionDenied = ToolScopeDenied


# ---------------------------------------------------------------------------
# Audit outbox protocol — mirrors the surface used by REST routes (see
# ``bsage.garden.audit_outbox.AiosqliteAuditOutbox``) without forcing the
# tooling layer to depend on the concrete implementation.
# ---------------------------------------------------------------------------
@runtime_checkable
class AuditOutboxLike(Protocol):
    is_open: bool

    async def insert_event(self, event: Any) -> None:  # pragma: no cover - protocol
        ...


# ---------------------------------------------------------------------------
# Context + Tool primitive
# ---------------------------------------------------------------------------
@dataclass
class ToolContext:
    """Runtime context handed to every tool handler.

    ``user`` mirrors :class:`bsvibe_authz.User`. The dispatcher never
    inspects internal fields directly — only ``user.id``, ``user.email``,
    ``user.is_service``, ``user.is_demo``, ``user.active_tenant_id`` and
    ``user.app_metadata`` (consumed inside ``check_tenant_permission``) —
    so a duck-typed test fixture works without dragging in the real
    authz package.

    ``fga`` / ``cache`` are the OpenFGA client + permission cache used by
    the Tier 5 permission check. They are optional: when absent the
    dispatcher lazily resolves the process-wide singletons from
    ``bsvibe_authz`` (mirroring how the REST ``require_permission``
    dependency injects them). ``settings`` is the
    ``bsvibe_authz.Settings`` the OpenFGA check reads
    (``openfga_api_url`` decides permissive mode); when absent the
    dispatcher falls back to ``bsvibe_authz.get_settings()`` — the same
    Settings the REST app's ``get_settings_dep`` resolves.
    """

    user: Any | None = None
    audit_outbox: AuditOutboxLike | None = None
    state: Any | None = None
    settings: Any | None = None
    fga: Any | None = None
    cache: Any | None = None
    request_id: str | None = None


ToolHandler = Callable[[BaseModel, ToolContext], Awaitable[BaseModel]]


@dataclass
class Tool:
    """First-class MCP tool definition.

    ``required_permission`` is a ``<product>.<resource>.<action>``
    dot-string (e.g. ``"bsage.canonicalization.apply"``) checked against
    OpenFGA — every value MUST be a row in the bsvibe-authz permission
    matrix. ``None`` means the tool is open to any authenticated
    principal (the SSE / stdio connection is already authenticated).
    """

    name: str
    description: str
    input_schema: type[BaseModel]
    output_schema: type[BaseModel]
    handler: ToolHandler
    required_permission: str | None = None
    audit_event: str | None = None


# ---------------------------------------------------------------------------
# Registry / dispatcher
# ---------------------------------------------------------------------------
class ToolRegistry:
    """In-process registry + dispatcher for first-class MCP tools.

    Mounted from both transports (HTTP ``/mcp``, stdio ``bsage mcp serve
    --transport stdio``) so that domain + admin tools share one
    catalog.
    """

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    # -- registration -------------------------------------------------------
    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"tool {tool.name!r} already registered")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)

    def names(self) -> list[str]:
        return sorted(self._tools.keys())

    # -- ListTools ----------------------------------------------------------
    def list_tools(self) -> list[McpTool]:
        """Return MCP-wire ``Tool`` definitions for every registered tool."""
        return [
            McpTool(
                name=t.name,
                description=t.description,
                inputSchema=_pydantic_to_json_schema(t.input_schema),
            )
            for t in self._tools.values()
        ]

    # -- CallTool -----------------------------------------------------------
    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None,
        ctx: ToolContext,
    ) -> dict[str, Any]:
        """Validate args → enforce scope → run → validate output → audit emit.

        Returns the validated output as a JSON-safe ``dict``. The MCP
        transport wraps that into a ``TextContent`` payload — that
        translation lives in the transport layer, not here.
        """
        tool = self._tools.get(name)
        if tool is None:
            raise ToolError(f"unknown tool: {name}")

        # 1. Input validation.
        try:
            args_model = tool.input_schema.model_validate(arguments or {})
        except ValidationError as exc:
            # Pydantic errors are user-facing (caller-visible) and safe
            # to surface — they describe the schema violation, not
            # internal state.
            raise ToolError(f"invalid arguments for {name}: {exc.errors()}") from exc

        # 2. Permission enforcement (Tier 5 — OpenFGA, shared with REST).
        await _enforce_permission(tool, ctx)

        # 3. Handler invocation — wrap any internal failure so the wire
        #    response never leaks implementation detail.
        try:
            output = await tool.handler(args_model, ctx)
        except ToolError:
            # Handlers may raise dispatcher-shaped errors directly;
            # propagate them unchanged.
            raise
        except Exception as exc:  # noqa: BLE001 — boundary translation
            # Round 4 Finding 21: surface the exception class so an LLM
            # caller can distinguish "PermissionError" vs "FileNotFoundError"
            # vs "ValidationError" and self-correct. The exception MESSAGE
            # stays redacted (it can carry DB columns, file paths, secrets);
            # class names are public stdlib/lib types and safe to expose.
            # The full traceback + message stays in the structured log.
            logger.exception(
                "mcp_tool_handler_failed",
                tool=name,
                error_type=type(exc).__name__,
            )
            raise ToolError(f"tool {name!r} failed: {type(exc).__name__}") from exc

        # 4. Output validation.
        try:
            output_model = tool.output_schema.model_validate(output)
        except ValidationError as exc:
            logger.warning(
                "mcp_tool_output_invalid",
                tool=name,
                errors=exc.errors(),
            )
            raise ToolError(f"tool {name!r} produced invalid output") from exc

        # 5. Audit emit (best-effort, never breaks the call).
        if tool.audit_event is not None:
            await _safe_audit_emit(tool, ctx)

        return output_model.model_dump(mode="json")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _pydantic_to_json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Render a Pydantic model's JSON Schema for the MCP wire."""
    schema = model.model_json_schema()
    # Pydantic returns a $defs-style schema; keep it intact — the MCP
    # SDK's clients (and Claude Desktop) follow JSON Schema 2020-12.
    if "type" not in schema:
        schema["type"] = "object"
    return schema


def _resolve_authz(ctx: ToolContext) -> tuple[Any, Any, Any]:
    """Resolve ``(settings, fga, cache)`` for the permission check.

    Prefers values already on ``ctx`` (the SSE/stdio transport may inject
    them); otherwise lazily resolves the process-wide ``bsvibe_authz``
    singletons — the same client + cache the REST ``require_permission``
    dependency uses, so MCP and REST share one OpenFGA client and one
    30s permission cache per process.
    """
    from bsvibe_authz import get_openfga_client, get_permission_cache, get_settings

    settings = ctx.settings
    if settings is None or not hasattr(settings, "openfga_api_url"):
        # ``ctx.settings`` is BSage's own Settings on most call paths —
        # the OpenFGA check needs bsvibe_authz.Settings. Fall back to the
        # library default (env-loaded, same as REST's get_settings_dep).
        settings = get_settings()

    # Permissive mode — OpenFGA not deployed. ``check_tenant_permission``
    # short-circuits to allow before touching ``fga``, so do not pay the
    # cost of constructing an OpenFGA client that will never be called.
    if not getattr(settings, "openfga_api_url", ""):
        return settings, ctx.fga, ctx.cache

    fga = ctx.fga if ctx.fga is not None else get_openfga_client(settings)
    cache = ctx.cache if ctx.cache is not None else get_permission_cache(settings)
    return settings, fga, cache


async def _enforce_permission(tool: Tool, ctx: ToolContext) -> None:
    """Enforce ``tool.required_permission`` via OpenFGA (Tier 5).

    Unified with the gateway routes: the same
    ``bsvibe_authz.check_tenant_permission`` call backs both this MCP
    dispatcher and the REST ``require_permission`` dependency, so one
    OpenFGA model is the source of truth for both surfaces.

    A tool with no ``required_permission`` is open to any authenticated
    principal (the SSE/stdio connection is already authenticated).
    Anonymous callers are denied on permissioned tools. The check is
    permissive (allow) for demo sessions and when OpenFGA is unconfigured
    — identical posture to ``require_permission``.
    """
    permission = tool.required_permission
    if not permission:
        return
    user = ctx.user
    if user is None:
        raise ToolScopeDenied(f"tool {tool.name!r} requires authentication")

    from bsvibe_authz import check_tenant_permission

    settings, fga, cache = _resolve_authz(ctx)
    allowed = await check_tenant_permission(
        user,
        permission,
        fga=fga,
        cache=cache,
        settings=settings,
    )
    if not allowed:
        raise ToolScopeDenied(
            f"tool {tool.name!r} requires permission: {permission}",
        )


async def _safe_audit_emit(tool: Tool, ctx: ToolContext) -> None:
    """Emit ``tool.audit_event`` via the audit outbox.

    Failures are swallowed — identical contract to
    :func:`bsage.garden.audit_outbox.safe_emit` so an outage in the
    audit pipeline cannot break a successful tool call.

    Sensitive arguments are NOT echoed in the event payload — only the
    tool name + actor land on the wire. Handlers wanting richer audit
    payloads should emit their own typed events from inside the
    handler body, identical to how REST routes do.
    """
    outbox = ctx.audit_outbox
    if outbox is None or not getattr(outbox, "is_open", False):
        return
    try:
        from bsvibe_audit import AuditActor, AuditResource
        from bsvibe_audit.events import AuditEventBase

        actor = _actor_from_user(ctx.user, AuditActor)
        event = AuditEventBase(
            event_type=tool.audit_event or f"bsage.mcp.{tool.name}.invoked",
            actor=actor,
            tenant_id=getattr(ctx.user, "active_tenant_id", None),
            resource=AuditResource(type="mcp_tool", id=tool.name),
            data={"tool": tool.name},
        )
        await outbox.insert_event(event)
    except Exception:  # noqa: BLE001 - audit must never break the call
        logger.warning(
            "mcp_audit_emit_failed",
            tool=tool.name,
            event_type=tool.audit_event,
            exc_info=True,
        )


def _actor_from_user(user: Any, actor_cls: type) -> Any:
    """Build an AuditActor from a principal — system fallback when None."""
    if user is None:
        return actor_cls(type="system", id="bsage")
    pid = getattr(user, "id", None) or "anonymous"
    email = getattr(user, "email", None)
    actor_type = "service" if getattr(user, "is_service", False) else "user"
    return actor_cls(
        type=actor_type,
        id=str(pid),
        email=email if isinstance(email, str) else None,
    )


__all__ = [
    "AuditOutboxLike",
    "Tool",
    "ToolContext",
    "ToolError",
    "ToolHandler",
    "ToolPermissionDenied",
    "ToolRegistry",
    "ToolScopeDenied",
]
