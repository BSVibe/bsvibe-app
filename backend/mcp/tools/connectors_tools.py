"""Connector tools — UI-parity setup surface (Lift D3a).

Wraps the workspace-scoped connector-binding CRUD that the PWA's Settings →
Connectors tab exposes via ``/api/v1/connectors``. Handlers are thin: they
touch the same :class:`backend.connectors.db.ConnectorAccountRow` rows the
REST endpoints persist, and the create / delete tools mirror the REST
endpoint behaviour 1:1 — encrypt the signing secret with
:class:`backend.router.accounts.crypto.CredentialCipher`, mint an
unguessable ``webhook_token``, and return it ONLY in the create response
(like an API key, never again).

The ``import_now`` tool re-uses the REST :class:`ImportDispatcher` (the
same primitive ``POST /api/v1/connectors/{id}/import`` dispatches through).
Pulling the dispatcher in here keeps MCP + REST on one bulk-import code
path so the founder doesn't see drift between the PWA's "Import now"
button and the MCP tool. The factory builds the dispatcher lazily.

Scopes follow the convention established by the existing 16 tools:
``mcp:read`` for inspection, ``mcp:write`` for mutations (create / delete
/ import) — including the ones that carry a secret on the wire, the same
scope :mod:`safe_mode_tools` uses for irreversible actions.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, RootModel
from sqlalchemy import select

from backend.api.v1.connectors import get_import_dispatcher
from backend.connectors.db import ConnectorAccountRow
from backend.connectors.kinds import (
    connector_kind,
    import_action_for,
    is_inbound,
    is_known_connector,
)
from backend.extensions.plugin.base import PluginRunError
from backend.extensions.plugin.webhook_registry import get_default_registry
from backend.mcp.api import Tool, ToolContext, ToolError, ToolRegistry
from backend.router.accounts.crypto import CredentialCipher, _key_from_settings
from backend.workflow.application.delivery.connector_dispatch import OUTBOUND_EVENT_BUILDERS

_TOKEN_BYTES = 32
_IMPORTED_COUNT_KEYS = (
    "notes_count",
    "conversations_count",
    "pages_count",
    "imported_count",
)


class _Envelope(RootModel[Any]):
    """Permissive output envelope — preserves the natural JSON shape."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _token_hint(webhook_token: str) -> str:
    """Last 4 chars only — enough to recognise, not enough to use."""
    return f"...{webhook_token[-4:]}"


def _row_to_dict(row: ConnectorAccountRow) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "workspace_id": str(row.workspace_id),
        "connector": row.connector,
        "external_ref": row.external_ref,
        "is_active": row.is_active,
        "delivery_config": row.delivery_config,
        "token_hint": _token_hint(row.webhook_token),
        "kind": connector_kind(row.connector),
        "last_import_at": row.last_import_at.isoformat() if row.last_import_at else None,
        "last_import_count": row.last_import_count,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def _is_registerable_connector(name: str) -> bool:
    """Same gate the REST :class:`ConnectorCreate` validator applies."""
    return (
        is_known_connector(name)
        or get_default_registry().is_known(name)
        or name in OUTBOUND_EVENT_BUILDERS
    )


def _webhook_url(connector: str, webhook_token: str) -> str:
    return f"/api/webhooks/{connector}/{webhook_token}"


def _resolve_imported_count(detail: dict[str, Any]) -> int:
    for key in _IMPORTED_COUNT_KEYS:
        value = detail.get(key)
        if isinstance(value, int):
            return value
    return 0


async def _resolve_connector(ctx: ToolContext, connector_id: uuid.UUID) -> ConnectorAccountRow:
    row = await ctx.session.get(ConnectorAccountRow, connector_id)
    if row is None or row.workspace_id != ctx.principal.workspace_id:
        raise ToolError(f"connector not found: {connector_id}")
    return row


# ---------------------------------------------------------------------------
# bsvibe_connectors_list
# ---------------------------------------------------------------------------
class ConnectorsListInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


async def _h_list(_args: ConnectorsListInput, ctx: ToolContext) -> Any:
    rows = (
        (
            await ctx.session.execute(
                select(ConnectorAccountRow)
                .where(ConnectorAccountRow.workspace_id == ctx.principal.workspace_id)
                .order_by(ConnectorAccountRow.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return _Envelope([_row_to_dict(r) for r in rows])


# ---------------------------------------------------------------------------
# bsvibe_connectors_show
# ---------------------------------------------------------------------------
class ConnectorsShowInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    connector_id: uuid.UUID


async def _h_show(args: ConnectorsShowInput, ctx: ToolContext) -> Any:
    row = await _resolve_connector(ctx, args.connector_id)
    return _Envelope(_row_to_dict(row))


# ---------------------------------------------------------------------------
# bsvibe_connectors_create
# ---------------------------------------------------------------------------
class ConnectorsCreateInput(BaseModel):
    """Inbound — plaintext ``signing_secret`` is encrypted at the boundary.

    Mirrors :class:`backend.api.v1.connectors.ConnectorCreate` 1:1.
    """

    model_config = ConfigDict(extra="forbid")

    connector: str = Field(..., min_length=1, max_length=64)
    signing_secret: str = Field(..., min_length=1, max_length=1024)
    external_ref: str | None = Field(default=None, max_length=255)
    delivery_config: dict[str, Any] = Field(default_factory=dict)


class ConnectorsCreateOutput(BaseModel):
    """The ONLY place the full webhook_token + URL are returned."""

    model_config = ConfigDict(extra="forbid")

    id: str
    connector: str
    external_ref: str | None
    is_active: bool
    delivery_config: dict[str, Any]
    webhook_token: str
    webhook_url: str
    kind: str | None
    created_at: str | None


async def _h_create(args: ConnectorsCreateInput, ctx: ToolContext) -> Any:
    if not _is_registerable_connector(args.connector):
        raise ToolError(f"unknown connector: {args.connector!r}")
    cipher = CredentialCipher(_key_from_settings())
    webhook_token = secrets.token_urlsafe(_TOKEN_BYTES)
    row = ConnectorAccountRow(
        id=uuid.uuid4(),
        workspace_id=ctx.principal.workspace_id,
        connector=args.connector,
        webhook_token=webhook_token,
        signing_secret_ciphertext=cipher.encrypt(args.signing_secret),
        external_ref=args.external_ref,
        delivery_config=args.delivery_config,
        is_active=True,
    )
    ctx.session.add(row)
    await ctx.session.commit()
    return ConnectorsCreateOutput(
        id=str(row.id),
        connector=row.connector,
        external_ref=row.external_ref,
        is_active=row.is_active,
        delivery_config=row.delivery_config,
        webhook_token=webhook_token,
        webhook_url=_webhook_url(row.connector, webhook_token),
        kind=connector_kind(row.connector),
        created_at=row.created_at.isoformat() if row.created_at else None,
    )


# ---------------------------------------------------------------------------
# bsvibe_connectors_delete — soft revoke, matches the REST DELETE
# ---------------------------------------------------------------------------
class ConnectorsDeleteInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    connector_id: uuid.UUID


class ConnectorsDeleteOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revoked: bool
    connector_id: str


async def _h_delete(args: ConnectorsDeleteInput, ctx: ToolContext) -> Any:
    row = await _resolve_connector(ctx, args.connector_id)
    row.is_active = False
    await ctx.session.commit()
    return ConnectorsDeleteOutput(revoked=True, connector_id=str(args.connector_id))


# ---------------------------------------------------------------------------
# bsvibe_connectors_import_now — Lift B inbound bulk import via MCP
# ---------------------------------------------------------------------------
class ConnectorsImportNowInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    connector_id: uuid.UUID


class ConnectorsImportNowOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    imported_count: int
    last_import_at: str
    detail: dict[str, Any]


async def _build_dispatcher(ctx: ToolContext) -> Any:
    """Build (or read from ``ctx.extras``) an :class:`ImportDispatcher`.

    Tests inject a pre-built dispatcher into ``ctx.extras["import_dispatcher"]``
    (duck-typed — must expose ``async import_for(row=, workspace_id=)``) so
    the unit run never touches the plugin loader / filesystem / KMS.
    Production builds one via the same factory the REST route uses.
    """
    cached = ctx.extras.get("import_dispatcher") if ctx.extras else None
    if cached is not None:
        return cached
    return await get_import_dispatcher()


async def _h_import_now(args: ConnectorsImportNowInput, ctx: ToolContext) -> Any:
    row = await _resolve_connector(ctx, args.connector_id)
    if not row.is_active:
        raise ToolError(f"connector not found: {args.connector_id}")
    if not is_inbound(row.connector):
        raise ToolError(f"connector {row.connector!r} is outbound-only — no bulk import available")
    if import_action_for(row.connector) is None:
        raise ToolError(
            f"connector {row.connector!r} has no bulk-import action — "
            f"its inbound path is webhook-driven (push-only)"
        )
    dispatcher = await _build_dispatcher(ctx)
    try:
        detail = await dispatcher.import_for(row=row, workspace_id=ctx.principal.workspace_id)
    except PluginRunError as exc:
        raise ToolError(f"import failed: {exc}") from exc
    imported_count = _resolve_imported_count(detail)
    now = datetime.now(tz=UTC)
    row.last_import_at = now
    row.last_import_count = imported_count
    await ctx.session.commit()
    return ConnectorsImportNowOutput(
        imported_count=imported_count,
        last_import_at=now.isoformat(),
        detail=detail,
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------
def register_connectors_tools(registry: ToolRegistry) -> None:
    registry.register(
        Tool(
            name="bsvibe_connectors_list",
            description=(
                "List connector bindings in the active workspace, newest first. "
                "Token hints are masked — only the last 4 chars are returned."
            ),
            input_schema=ConnectorsListInput,
            output_schema=_Envelope,
            handler=_h_list,
            required_scopes=("mcp:read",),
        )
    )
    registry.register(
        Tool(
            name="bsvibe_connectors_show",
            description="Show one connector binding by id.",
            input_schema=ConnectorsShowInput,
            output_schema=_Envelope,
            handler=_h_show,
            required_scopes=("mcp:read",),
        )
    )
    registry.register(
        Tool(
            name="bsvibe_connectors_create",
            description=(
                "Bind a new connector for the active workspace. The signing "
                "secret is encrypted at the boundary; the full webhook URL + "
                "token are returned ONLY in this response (like an API key)."
            ),
            input_schema=ConnectorsCreateInput,
            output_schema=ConnectorsCreateOutput,
            handler=_h_create,
            required_scopes=("mcp:write",),
            audit_event="bsvibe.mcp.connectors_create.invoked",
        )
    )
    registry.register(
        Tool(
            name="bsvibe_connectors_delete",
            description=(
                "Soft-revoke a connector binding — flips `is_active` False. The "
                "public ingress 404s on revoked bindings immediately."
            ),
            input_schema=ConnectorsDeleteInput,
            output_schema=ConnectorsDeleteOutput,
            handler=_h_delete,
            required_scopes=("mcp:write",),
            audit_event="bsvibe.mcp.connectors_delete.invoked",
        )
    )
    registry.register(
        Tool(
            name="bsvibe_connectors_import_now",
            description=(
                "Trigger an inbound bulk import for an inbound/both connector "
                "(obsidian / claude / gpt / notion). Synchronous — returns the "
                "imported count and timestamp once the import completes."
            ),
            input_schema=ConnectorsImportNowInput,
            output_schema=ConnectorsImportNowOutput,
            handler=_h_import_now,
            required_scopes=("mcp:write",),
            audit_event="bsvibe.mcp.connectors_import_now.invoked",
        )
    )


__all__ = ["register_connectors_tools"]
