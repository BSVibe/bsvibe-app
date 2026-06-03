"""First-class MCP API primitives — Lift D2.

Mirrors the FastAPI router contract for the in-process MCP surface:

1. validate input against a Pydantic schema,
2. enforce OAuth scopes against the authenticated principal,
3. invoke the typed handler,
4. validate output,
5. (optionally) emit one audit event.

The dispatcher is intentionally a small replica of how FastAPI behaves so
that tool authors can think in REST-shaped primitives even though the
wire is JSON-over-MCP. Authentication is performed by the Streamable HTTP
transport (:mod:`backend.mcp.streamable_http`) which verifies the ES256
Bearer access token issued by the embedded OAuth server (Lift D1) and
stashes the resolved :class:`McpPrincipal` on a contextvar; the
dispatcher reads that back when building :class:`ToolContext`.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

import structlog
from mcp.types import Tool as McpTool
from pydantic import BaseModel, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

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
    """Raised when the principal lacks a required OAuth scope.

    Distinct from a generic ToolError so callers (and the Streamable
    HTTP transport) can map it to a 403 in the MCP error frame.
    """


# ---------------------------------------------------------------------------
# Principal — embedded OAuth (D1) materialised view.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class McpPrincipal:
    """The verified OAuth principal for one MCP request.

    Built by :mod:`backend.mcp.auth` from the access-token JWT claims and
    the row lookup behind it. The dispatcher reads ``scopes`` to enforce
    ``Tool.required_scopes``; handlers read ``user_id`` / ``workspace_id``
    to scope every repository call.
    """

    user_id: uuid.UUID
    workspace_id: uuid.UUID
    client_id: str
    scopes: frozenset[str]
    jti: uuid.UUID

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes


# ---------------------------------------------------------------------------
# Audit outbox protocol — matches the REST surface's audit pipeline.
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
    """Per-call context handed to every tool handler.

    The MCP dispatcher constructs one of these per ``CallTool`` request.
    Handlers depend on it for the principal, the workspace-scoped DB
    session, and (optionally) an audit outbox.
    """

    principal: McpPrincipal
    session: AsyncSession
    audit_outbox: AuditOutboxLike | None = None
    request_id: str | None = None
    extras: dict[str, Any] = field(default_factory=dict)


# Handlers may return a ``BaseModel`` or a plain ``dict`` — the
# dispatcher's ``model_validate`` accepts both.
ToolHandler = Callable[[Any, ToolContext], Awaitable[Any]]


@dataclass
class Tool:
    """First-class MCP tool definition.

    ``required_scopes`` is the OAuth-scope guard. The dispatcher denies
    when *any* declared scope is absent from the principal. An empty
    tuple means "any authenticated principal" (the principal still has
    to verify — the Streamable HTTP transport already enforced that
    before the dispatcher even ran).
    """

    name: str
    description: str
    input_schema: type[BaseModel]
    output_schema: type[BaseModel]
    handler: ToolHandler
    required_scopes: tuple[str, ...] = ()
    audit_event: str | None = None


# ---------------------------------------------------------------------------
# Registry / dispatcher
# ---------------------------------------------------------------------------
class ToolRegistry:
    """In-process registry + dispatcher for first-class MCP tools."""

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
        """Validate args → enforce scopes → run → validate output → audit emit."""
        tool = self._tools.get(name)
        if tool is None:
            raise ToolError(f"unknown tool: {name}")

        # 1. Input validation.
        try:
            args_model = tool.input_schema.model_validate(arguments or {})
        except ValidationError as exc:
            raise ToolError(f"invalid arguments for {name}: {exc.errors()}") from exc

        # 2. Scope enforcement.
        _enforce_scopes(tool, ctx)

        # 3. Handler invocation — wrap any internal failure so the wire
        #    response never leaks implementation detail.
        try:
            output = await tool.handler(args_model, ctx)
        except ToolError:
            raise
        except Exception as exc:  # noqa: BLE001 — boundary translation
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
    if "type" not in schema:
        schema["type"] = "object"
    return schema


def _enforce_scopes(tool: Tool, ctx: ToolContext) -> None:
    """Require every declared scope to be present on the principal."""
    if not tool.required_scopes:
        return
    missing = [s for s in tool.required_scopes if not ctx.principal.has_scope(s)]
    if missing:
        raise ToolScopeDenied(
            f"tool {tool.name!r} requires scope(s) {missing} — token has {sorted(ctx.principal.scopes)}"
        )


async def _safe_audit_emit(tool: Tool, ctx: ToolContext) -> None:
    """Emit ``tool.audit_event`` via the audit outbox.

    Failures are swallowed — an audit-pipeline outage cannot break a
    successful tool call. The payload carries only the tool name + the
    actor; richer event payloads are the handler's job (matches the REST
    audit convention).
    """
    outbox = ctx.audit_outbox
    if outbox is None or not getattr(outbox, "is_open", False):
        return
    try:
        from plugin.audit.events import (  # noqa: PLC0415 — lazy to avoid cycle
            AuditActor,
            AuditEventBase,
            AuditResource,
        )

        actor = AuditActor(
            type="user",
            id=str(ctx.principal.user_id),
            email=None,
        )
        event = AuditEventBase(
            event_type=tool.audit_event or f"bsvibe.mcp.{tool.name}.invoked",
            actor=actor,
            tenant_id=None,
            resource=AuditResource(type="mcp_tool", id=tool.name),
            data={"tool": tool.name, "client_id": ctx.principal.client_id},
        )
        await outbox.insert_event(event)
    except Exception:  # noqa: BLE001 — audit must never break the call
        logger.warning(
            "mcp_audit_emit_failed",
            tool=tool.name,
            event_type=tool.audit_event,
            exc_info=True,
        )


__all__ = [
    "AuditOutboxLike",
    "McpPrincipal",
    "Tool",
    "ToolContext",
    "ToolError",
    "ToolHandler",
    "ToolRegistry",
    "ToolScopeDenied",
]
