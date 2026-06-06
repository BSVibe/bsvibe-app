"""Workspace tools — UI-parity surface (Lift D3c).

Wraps the founder-facing workspace surface the PWA's
:mod:`apps/pwa/components/settings/GeneralTab` drives via
``/api/v1/workspace`` (singular):

* ``GET    /api/v1/workspace`` → :func:`bsvibe_workspace_get`
* ``PATCH  /api/v1/workspace`` (name only) → :func:`bsvibe_workspace_rename`

The PWA exposes ONLY name editing today; ``safe_mode``, ``region``, and
``audit_retention_days`` exist on the row + the REST PATCH but have no
PWA UI, so per the parity contract they are NOT shipped as MCP tools in
this lift. ``get`` does surface those fields read-only because the GET
endpoint already returns them — keeping the read shape 1:1 with the
REST payload (an LLM can therefore inspect Safe Mode state, just not flip
it).

Workspace settings are sensitive (deletion, retention, region cutovers
all hang off them in the future); ``rename`` lands on ``mcp:admin`` to
keep the bar high while a ``mcp:read`` token can still read the state.
"""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from backend.identity.infrastructure.repositories import SqlAlchemyWorkspaceRepository
from backend.mcp.api import Tool, ToolContext, ToolError, ToolRegistry
from backend.router.infrastructure.repositories import SqlAlchemyModelAccountRepository


# ---------------------------------------------------------------------------
# bsvibe_workspace_get
# ---------------------------------------------------------------------------
class WorkspaceGetInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WorkspaceGetOutput(BaseModel):
    """Mirror of :class:`WorkspaceOut` (REST) + the additional fields the
    plural ``/api/v1/workspaces/{id}`` GET surfaces.

    Read-only — the only field the PWA mutates today is ``name`` (see
    :func:`bsvibe_workspace_rename`). The other fields are visible so an
    LLM can inspect Safe Mode / region / retention without being able to
    flip them through MCP (parity rule).
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    region: str
    safe_mode: bool
    audit_retention_days: int | None = None
    # Lift E1 — workspace-default ModelAccount for the new
    # :class:`backend.dispatch.resolver.ModelAccountResolver` fallback.
    # ``None`` = the founder has not picked one yet.
    default_account_id: str | None = None


async def _h_get(_args: WorkspaceGetInput, ctx: ToolContext) -> Any:
    repo = SqlAlchemyWorkspaceRepository(ctx.session)
    row = await repo.get(ctx.principal.workspace_id)
    if row is None:
        raise ToolError(f"workspace not found: {ctx.principal.workspace_id}")
    return WorkspaceGetOutput(
        id=str(row.id),
        name=row.name,
        region=row.region,
        safe_mode=row.safe_mode,
        audit_retention_days=row.audit_retention_days,
        default_account_id=(
            str(row.default_account_id) if row.default_account_id is not None else None
        ),
    )


# ---------------------------------------------------------------------------
# bsvibe_workspace_rename
# ---------------------------------------------------------------------------
class WorkspaceRenameInput(BaseModel):
    """Mirror of the ``name``-only PATCH body the PWA Settings → General sends.

    Trim is applied server-side (same as REST). ``min_length=1`` rejects a
    blank-after-trim attempt at validation time.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)


async def _h_rename(args: WorkspaceRenameInput, ctx: ToolContext) -> Any:
    trimmed = args.name.strip()
    if not trimmed:
        raise ToolError("name must not be blank")
    repo = SqlAlchemyWorkspaceRepository(ctx.session)
    row = await repo.get(ctx.principal.workspace_id)
    if row is None:
        raise ToolError(f"workspace not found: {ctx.principal.workspace_id}")
    row.name = trimmed
    await ctx.session.commit()
    return WorkspaceGetOutput(
        id=str(row.id),
        name=row.name,
        region=row.region,
        safe_mode=row.safe_mode,
        audit_retention_days=row.audit_retention_days,
        default_account_id=(
            str(row.default_account_id) if row.default_account_id is not None else None
        ),
    )


# ---------------------------------------------------------------------------
# bsvibe_workspace_set_default_account — Lift E1 MCP parity
# ---------------------------------------------------------------------------
class WorkspaceSetDefaultAccountInput(BaseModel):
    """Set (or clear) the workspace's default ModelAccount.

    Used by the new :class:`backend.dispatch.resolver.ModelAccountResolver`
    as the fallback when no routing rule matches a caller_id. ``null``
    clears the column (resolver hard-fails on unmatched rules instead);
    a UUID picks an active ModelAccount in this workspace.
    """

    model_config = ConfigDict(extra="forbid")

    account_id: uuid.UUID | None = Field(
        default=None,
        description=(
            "The ModelAccount.id to set as the workspace default, or null "
            "to unset. The account must be active and in this workspace."
        ),
    )


async def _h_set_default_account(args: WorkspaceSetDefaultAccountInput, ctx: ToolContext) -> Any:
    repo = SqlAlchemyWorkspaceRepository(ctx.session)
    row = await repo.get(ctx.principal.workspace_id)
    if row is None:
        raise ToolError(f"workspace not found: {ctx.principal.workspace_id}")
    if args.account_id is not None:
        accounts_repo = SqlAlchemyModelAccountRepository(ctx.session)
        active = await accounts_repo.list_active_for_workspace(
            workspace_id=ctx.principal.workspace_id
        )
        if not any(a.id == args.account_id for a in active):
            raise ToolError("account_id must reference an active ModelAccount in this workspace")
    row.default_account_id = args.account_id
    await ctx.session.commit()
    return WorkspaceGetOutput(
        id=str(row.id),
        name=row.name,
        region=row.region,
        safe_mode=row.safe_mode,
        audit_retention_days=row.audit_retention_days,
        default_account_id=(
            str(row.default_account_id) if row.default_account_id is not None else None
        ),
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------
def register_workspace_tools(registry: ToolRegistry) -> None:
    registry.register(
        Tool(
            name="bsvibe_workspace_get",
            description=(
                "Read the active workspace's id, name, region, safe_mode state, "
                "and audit retention. Mirrors the PWA Settings → General read."
            ),
            input_schema=WorkspaceGetInput,
            output_schema=WorkspaceGetOutput,
            handler=_h_get,
            required_scopes=("mcp:read",),
        )
    )
    registry.register(
        Tool(
            name="bsvibe_workspace_rename",
            description=(
                "Rename the active workspace. Mirrors the PWA Settings → "
                "General 'Workspace name' edit. `mcp:admin` scope — the only "
                "PWA-exposed workspace mutation, kept at a high bar."
            ),
            input_schema=WorkspaceRenameInput,
            output_schema=WorkspaceGetOutput,
            handler=_h_rename,
            required_scopes=("mcp:admin",),
            audit_event="bsvibe.mcp.workspace_rename.invoked",
        )
    )
    registry.register(
        Tool(
            name="bsvibe_workspace_set_default_account",
            description=(
                "Set (or clear with null) the workspace's default ModelAccount. "
                "The new ModelAccountResolver (Lift E1) falls back to this "
                "account when no routing rule matches a caller_id. Mirrors the "
                "PWA Settings → Models 'Default for this workspace' picker. "
                "`mcp:admin` scope — a routing decision that affects every "
                "downstream LLM call."
            ),
            input_schema=WorkspaceSetDefaultAccountInput,
            output_schema=WorkspaceGetOutput,
            handler=_h_set_default_account,
            required_scopes=("mcp:admin",),
            audit_event="bsvibe.mcp.workspace_set_default_account.invoked",
        )
    )


__all__ = ["register_workspace_tools"]
