"""Connector MCP tools — parity with the PWA Settings → Connectors surface.

The MCP-UI parity rule: every action a founder can take in the PWA is also an
MCP tool, so an agentic client can wire connectors headlessly. The OAuth /
manifest flows still need ONE browser approval (no tool can click "Authorize"
on github.com), so those tools return a URL for the human to open; everything
else (list / create / revoke / status) is fully headless.

All tools act in the principal's workspace (``ctx.principal.workspace_id``).
Mutations require ``mcp:write``; reads require ``mcp:read``.
"""

from __future__ import annotations

import secrets
import uuid
from typing import Any

import structlog
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from backend.connectors.auth import service
from backend.connectors.auth.db import ConnectorOAuthTokenRow
from backend.connectors.db import ConnectorAccountRow
from backend.connectors.kinds import connector_kind, is_known_connector
from backend.mcp.api import Tool, ToolContext, ToolError, ToolRegistry
from backend.router.accounts.crypto import CredentialCipher, _key_from_settings

logger = structlog.get_logger(__name__)

_TOKEN_BYTES = 32


def _cipher() -> CredentialCipher:
    return CredentialCipher(_key_from_settings())


# ── list ───────────────────────────────────────────────────────────────


class ConnectorListInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ConnectorItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    connector: str
    is_active: bool
    external_ref: str | None
    delivery_config: dict[str, Any]
    kind: str | None
    oauth_account_label: str | None


class ConnectorListOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    connectors: list[ConnectorItem]


async def _h_list(_: ConnectorListInput, ctx: ToolContext) -> ConnectorListOutput:
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
    label_rows = (
        await ctx.session.execute(
            select(
                ConnectorOAuthTokenRow.connector_account_id,
                ConnectorOAuthTokenRow.account_label,
            ).where(ConnectorOAuthTokenRow.connector_account_id.in_([r.id for r in rows]))
        )
    ).all()
    labels: dict[uuid.UUID, str | None] = {aid: lbl for aid, lbl in label_rows}
    return ConnectorListOutput(
        connectors=[
            ConnectorItem(
                id=str(r.id),
                connector=r.connector,
                is_active=r.is_active,
                external_ref=r.external_ref,
                delivery_config=dict(r.delivery_config),
                kind=connector_kind(r.connector),
                oauth_account_label=labels.get(r.id),
            )
            for r in rows
        ]
    )


# ── create ─────────────────────────────────────────────────────────────


class ConnectorCreateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    connector: str = Field(..., min_length=1, max_length=64)
    signing_secret: str = Field(..., min_length=1, max_length=1024)
    external_ref: str | None = Field(default=None, max_length=255)
    delivery_config: dict[str, Any] = Field(default_factory=dict)


class ConnectorCreateOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    connector: str
    kind: str | None
    # One-time capability — the webhook URL + token are shown only here.
    webhook_url: str
    webhook_token: str


async def _h_create(args: ConnectorCreateInput, ctx: ToolContext) -> ConnectorCreateOutput:
    if not is_known_connector(args.connector):
        raise ToolError(f"unknown connector: {args.connector}")
    webhook_token = secrets.token_urlsafe(_TOKEN_BYTES)
    row = ConnectorAccountRow(
        id=uuid.uuid4(),
        workspace_id=ctx.principal.workspace_id,
        connector=args.connector,
        webhook_token=webhook_token,
        signing_secret_ciphertext=_cipher().encrypt(args.signing_secret),
        external_ref=args.external_ref,
        delivery_config=args.delivery_config,
        is_active=True,
    )
    ctx.session.add(row)
    await ctx.session.commit()
    return ConnectorCreateOutput(
        id=str(row.id),
        connector=row.connector,
        kind=connector_kind(row.connector),
        webhook_url=f"/api/webhooks/{row.connector}/{webhook_token}",
        webhook_token=webhook_token,
    )


# ── revoke ─────────────────────────────────────────────────────────────


class ConnectorRevokeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    connector_id: str = Field(..., max_length=64)


class ConnectorRevokeOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revoked: bool


async def _h_revoke(args: ConnectorRevokeInput, ctx: ToolContext) -> ConnectorRevokeOutput:
    try:
        connector_id = uuid.UUID(args.connector_id)
    except ValueError as exc:
        raise ToolError(f"invalid connector_id: {args.connector_id}") from exc
    row = await ctx.session.get(ConnectorAccountRow, connector_id)
    if row is None or row.workspace_id != ctx.principal.workspace_id:
        raise ToolError(f"connector not found: {args.connector_id}")
    row.is_active = False
    await ctx.session.commit()
    return ConnectorRevokeOutput(revoked=True)


# ── oauth start (returns a URL for the human to open) ───────────────────


class OAuthStartInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: str = Field(..., min_length=1, max_length=64)


class OAuthStartOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    authorize_url: str
    instructions: str


async def _h_oauth_start(args: OAuthStartInput, ctx: ToolContext) -> OAuthStartOutput:
    try:
        url = await service.begin_oauth_connect(
            ctx.session, provider=args.provider, workspace_id=ctx.principal.workspace_id
        )
    except service.UnknownProviderError as exc:
        raise ToolError(
            f"provider {args.provider!r} is not configured — set up its OAuth app first"
        ) from exc
    return OAuthStartOutput(
        authorize_url=url,
        instructions=(
            f"Open this URL in a browser to authorize {args.provider}. After you "
            "approve, the connection completes automatically — re-list connectors "
            "to confirm."
        ),
    )


# ── github app status ──────────────────────────────────────────────────


class GithubAppStatusInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GithubAppStatusOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    configured: bool
    app_slug: str | None
    html_url: str | None


async def _h_github_app_status(_: GithubAppStatusInput, ctx: ToolContext) -> GithubAppStatusOutput:
    data = await service.compute_github_app_status(ctx.session, cipher=_cipher())
    return GithubAppStatusOutput(**data)


# ── github app setup (manifest) URL ─────────────────────────────────────


class GithubAppSetupInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GithubAppSetupOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    post_url: str
    manifest: dict[str, Any]
    instructions: str


async def _h_github_app_setup(_: GithubAppSetupInput, ctx: ToolContext) -> GithubAppSetupOutput:
    data = await service.begin_github_app_manifest(
        ctx.session, workspace_id=ctx.principal.workspace_id
    )
    return GithubAppSetupOutput(
        post_url=data["post_url"],
        manifest=data["manifest"],
        instructions=(
            "POST the `manifest` (JSON) as a form field named 'manifest' to "
            "`post_url` in a browser, then approve 'Create GitHub App'. GitHub "
            "stores the credentials automatically; then use connector_oauth_start "
            "with provider='github'."
        ),
    )


def register_connector_tools(registry: ToolRegistry) -> None:
    """Register the connector parity tools onto ``registry``."""
    registry.register(
        Tool(
            name="bsvibe_connector_list",
            description="List the workspace's connectors (id, kind, OAuth connection state).",
            input_schema=ConnectorListInput,
            output_schema=ConnectorListOutput,
            handler=_h_list,
            required_scopes=("mcp:read",),
        )
    )
    registry.register(
        Tool(
            name="bsvibe_connector_create",
            description=(
                "Register a connector (webhook/secret-based). Returns the one-time "
                "webhook URL + token. For OAuth connectors use connector_oauth_start instead."
            ),
            input_schema=ConnectorCreateInput,
            output_schema=ConnectorCreateOutput,
            handler=_h_create,
            required_scopes=("mcp:write",),
            audit_event="bsvibe.mcp.connector.create.invoked",
        )
    )
    registry.register(
        Tool(
            name="bsvibe_connector_revoke",
            description="Soft-revoke a connector by id (the webhook ingress 404s thereafter).",
            input_schema=ConnectorRevokeInput,
            output_schema=ConnectorRevokeOutput,
            handler=_h_revoke,
            required_scopes=("mcp:write",),
            audit_event="bsvibe.mcp.connector.revoke.invoked",
        )
    )
    registry.register(
        Tool(
            name="bsvibe_connector_oauth_start",
            description=(
                "Begin 'Connect with X' for an OAuth connector (github/slack/notion/"
                "discord). Returns an authorize_url for the human to open in a browser."
            ),
            input_schema=OAuthStartInput,
            output_schema=OAuthStartOutput,
            handler=_h_oauth_start,
            required_scopes=("mcp:write",),
            audit_event="bsvibe.mcp.connector.oauth_start.invoked",
        )
    )
    registry.register(
        Tool(
            name="bsvibe_connector_github_app_status",
            description="Whether the GitHub App is set up (so 'Connect with GitHub' works).",
            input_schema=GithubAppStatusInput,
            output_schema=GithubAppStatusOutput,
            handler=_h_github_app_status,
            required_scopes=("mcp:read",),
        )
    )
    registry.register(
        Tool(
            name="bsvibe_connector_github_app_setup_url",
            description=(
                "Begin the GitHub App Manifest setup. Returns the GitHub POST target + "
                "manifest for the human to approve once (no manual env editing)."
            ),
            input_schema=GithubAppSetupInput,
            output_schema=GithubAppSetupOutput,
            handler=_h_github_app_setup,
            required_scopes=("mcp:write",),
            audit_event="bsvibe.mcp.connector.app_setup.invoked",
        )
    )


__all__ = ["register_connector_tools"]
