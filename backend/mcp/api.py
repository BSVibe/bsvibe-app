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
from typing import Any

import structlog
from mcp.types import Tool as McpTool
from pydantic import BaseModel, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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
    #: The ExecutionRun this token may act on — set ONLY on the short-lived token a
    #: dispatched executor task carries, so the agent's remote tools
    #: (:mod:`backend.mcp.tools.work_tools`) are bound to exactly one run's worktree.
    #: ``None`` on every ordinary token (the founder's editor, the CLI): those may read
    #: the workspace, but they may not reach into a run and edit code.
    #:
    #: It lives on the PRINCIPAL, never in the tool arguments: the blast radius of a
    #: leaked worker token has to be one run, and an argument the agent controls could
    #: redirect a write into another run's tree.
    run_id: uuid.UUID | None = None

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes


# 감사 아웃박스 프로토콜은 2026-09-23 에 삭제됐다(#1039). ``AuditOutboxLike`` 는
# ``is_open: bool`` 을 요구했는데 **어떤 구현도 그걸 세우지 않았다** — 그 이름은
# 코드 전체에서 선언과 검사 두 곳에만 있었다. 상상 속 인터페이스를 향해 설계된
# 감사가 49개 쓰기 툴을 통째로 침묵시켰다. 지금은 REST 와 같은 길을 쓴다:
# ``AuditEmitter().emit(event, session=ctx.session)``.


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
    request_id: str | None = None
    extras: dict[str, Any] = field(default_factory=dict)
    # The same factory the FastAPI app threads into REST handlers — used
    # by handlers that need to spawn background tasks with their own
    # session (e.g. ``products_create``'s post-commit bootstrap). Optional
    # so existing tests that build a ``ToolContext`` directly stay valid.
    session_factory: async_sessionmaker[AsyncSession] | None = None


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
    """Emit ``tool.audit_event`` into the caller's transaction (REST 와 같은 길).

    Failures are swallowed — an audit-pipeline outage cannot break a
    successful tool call. The payload carries only the tool name + the
    actor; richer event payloads are the handler's job (matches the REST
    audit convention).

    ⚠️ 2026-09-23 이전에는 이 함수가 **존재하지 않는 인터페이스**를 기다렸다.
    ``ctx.audit_outbox`` 가 ``is_open`` 인 것을 요구했는데, 그 이름은 코드 전체에서
    프로토콜 선언과 이 검사 **두 곳에만** 있었다 — 어떤 구현도 세우지 않는다.
    게다가 prod 의 유일한 ``ToolContext(`` 생성이 그 필드를 안 넘겨서 기본값
    ``None`` 으로 **첫 줄에서 리턴**했다. 예외도 경고도 없이.

    결과: 49개 쓰기 툴이 감사를 선언하는데 prod ``audit_outbox`` 의
    ``bsvibe.mcp.*`` 가 **0건**이었다(전체 5,571행 — 아웃박스는 살아 있었다).
    표면에서는 감사가 켜진 것과 완전히 똑같아 보였다. #1039
    """
    try:
        from plugin.audit.emitter import AuditEmitter  # noqa: PLC0415 — lazy to avoid cycle
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
        # REST 와 **같은 길**이다 — 호출자의 세션 안에 행을 남긴다.
        await AuditEmitter().emit(event, session=ctx.session)
        # ⚠️ 그리고 **직접 커밋한다.** 여기까지 오면 핸들러는 이미 자기 작업을
        #    커밋하고 끝났고(툴마다 `await ctx.session.commit()`), 이 뒤에는
        #    아무도 커밋하지 않는다 — 세션이 닫히며 감사 행이 조용히 사라진다.
        #    2026-09-23: 이 커밋이 없는 채로 배포했고 prod 는 여전히 0건이었다.
        #    유닛은 초록이었는데, **테스트가 call_tool 뒤에 커밋을 보태고 있었다**
        #    (프로덕션이 안 주는 것을 테스트가 준 것이다).
        await ctx.session.commit()
    except Exception:  # noqa: BLE001 — audit must never break the call
        logger.warning(
            "mcp_audit_emit_failed",
            tool=tool.name,
            event_type=tool.audit_event,
            exc_info=True,
        )


__all__ = [
    "McpPrincipal",
    "Tool",
    "ToolContext",
    "ToolError",
    "ToolHandler",
    "ToolRegistry",
    "ToolScopeDenied",
]
