"""Account tools — UI-parity read surface (Lift D3d).

Wraps the founder-facing personal-account discovery + workspace-
membership listing the PWA drives. Only what the PWA actually fetches
over REST is mirrored here — per the parity contract
([[bsvibe-mcp-ui-parity]]) PWA-derived-from-JWT data (display name, plan
placeholder, identity providers) is NOT a backend surface and therefore
NOT shipped as MCP tools.

REST counterparts:

* ``GET /api/v1/account`` (singular — :mod:`backend.api.v1.account`) —
  the active workspace's personal billing account (create-on-read).
  → :func:`bsvibe_account_get`.
* ``GET /api/v1/workspaces`` (plural —
  :mod:`backend.api.v1.workspaces` ``list_workspaces``) — every
  workspace the caller has an active membership in. → :func:`bsvibe_account_memberships_list`.

Both surfaces are ``mcp:read`` — nothing mutates.

Distinct from existing tools:

* :mod:`backend.mcp.tools.model_accounts_tools` wraps ``/api/v1/accounts``
  (plural — LLM provider credentials, "ModelAccount"). NOT the personal
  billing-account row.
* :mod:`backend.mcp.tools.workspace_tools` reads the ACTIVE workspace
  (the principal's ``workspace_id``); the listing here is "every workspace
  this user is a member of" — a different surface backing the PWA's
  workspace switcher.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from backend.identity.db import UserRow
from backend.identity.infrastructure.repositories import SqlAlchemyWorkspaceRepository
from backend.mcp.api import Tool, ToolContext, ToolError, ToolRegistry
from backend.router.accounts.account_service import ensure_personal_account


# ---------------------------------------------------------------------------
# bsvibe_account_get
# ---------------------------------------------------------------------------
class AccountGetInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AccountGetOutput(BaseModel):
    """Mirror of :class:`AccountResponse` (REST ``/api/v1/account``)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    workspace_id: str


async def _h_account_get(_args: AccountGetInput, ctx: ToolContext) -> Any:
    account = await ensure_personal_account(ctx.session, workspace_id=ctx.principal.workspace_id)
    await ctx.session.commit()
    return AccountGetOutput(id=str(account.id), workspace_id=str(account.workspace_id))


# ---------------------------------------------------------------------------
# bsvibe_account_memberships_list
# ---------------------------------------------------------------------------
class AccountMembershipsListInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MembershipEntryOut(BaseModel):
    """One workspace the caller has an active membership in.

    Shape mirrors :class:`WorkspaceResponse` (REST ``/api/v1/workspaces``)
    — the exact body the PWA's :func:`listWorkspaces` consumes for the
    workspace switcher.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    region: str
    safe_mode: bool
    created_at: datetime
    updated_at: datetime


class AccountMembershipsListOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    memberships: list[MembershipEntryOut]


async def _resolve_user_row(ctx: ToolContext) -> UserRow:
    """Look up the principal's :class:`UserRow` by ``user_id``.

    The principal's ``user_id`` is the local ``users.id`` (set by the
    auth layer when the OAuth Bearer is verified — see
    :mod:`backend.mcp.auth`). A row is always expected for an
    authenticated principal; absence is a wire-safe ``ToolError``.
    """
    row = await ctx.session.get(UserRow, ctx.principal.user_id)
    if row is None:
        # Defensive — the OAuth layer wouldn't issue a token for a
        # user_id that doesn't resolve, but the contract is "every
        # tool has a tight error path".
        raise ToolError(f"user not found: {ctx.principal.user_id}")
    return row


async def _h_memberships_list(_args: AccountMembershipsListInput, ctx: ToolContext) -> Any:
    user = await _resolve_user_row(ctx)
    repo = SqlAlchemyWorkspaceRepository(ctx.session)
    rows = await repo.list_for_user(user.id)
    return AccountMembershipsListOutput(
        memberships=[
            MembershipEntryOut(
                id=str(r.id),
                name=r.name,
                region=r.region,
                safe_mode=r.safe_mode,
                created_at=r.created_at,
                updated_at=r.updated_at,
            )
            for r in rows
        ]
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------
def register_account_tools(registry: ToolRegistry) -> None:
    registry.register(
        Tool(
            name="bsvibe_account_get",
            description=(
                "Read (or create-on-read) the active workspace's personal "
                "billing account id. Mirrors the PWA's `GET /api/v1/account` "
                "discovery call. Distinct from `bsvibe_model_accounts_list` "
                "(plural — LLM provider credentials)."
            ),
            input_schema=AccountGetInput,
            output_schema=AccountGetOutput,
            handler=_h_account_get,
            required_scopes=("mcp:read",),
        )
    )
    registry.register(
        Tool(
            name="bsvibe_account_memberships_list",
            description=(
                "List every workspace the calling user has an active "
                "membership in. Mirrors the PWA's `GET /api/v1/workspaces` "
                "(plural) — the workspace switcher's source of truth. "
                "Read-only; workspace creation / leave isn't a PWA-exposed "
                "MCP surface."
            ),
            input_schema=AccountMembershipsListInput,
            output_schema=AccountMembershipsListOutput,
            handler=_h_memberships_list,
            required_scopes=("mcp:read",),
        )
    )


__all__ = ["register_account_tools"]
