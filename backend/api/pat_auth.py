"""Auth resolver for the PAT endpoints — accepts either credential class.

A PAT exists so a browserless client can reach `/mcp`. Minting one from such a
host therefore cannot require the PWA's Supabase session JWT — the CLI holds an
ES256 access token issued by our own embedded OAuth server (``bsvibe login``,
including its ``--manual`` out-of-band flow). Both have to work.

**How the class is chosen.** :mod:`backend.api.bearer_auth` owns that branch
now — since #1017 the whole v1 gate accepts both classes, and these routes are
just one more caller of the same resolver, so there is one trust set rather
than a per-router opinion. What stays here is the rule that is specific to
minting credentials: the ``mcp:admin`` gate below.

**Why an access token needs ``mcp:admin`` to mint.** A credential that can mint
credentials is an escalation path, and the scopes already separate cleanly:

* ``bsvibe login`` registers a client asking for ``mcp:read mcp:write
  mcp:admin`` — a human at a browser approved it, so it may mint.
* :func:`backend.identity.oauth_service.issue_run_task_token` grants
  ``mcp:read mcp:write`` to a *dispatched executor task*. That agent must never
  be able to turn its 90-minute task credential into a permanent one.

The ``run_id`` check is belt-and-braces on the same rule: it holds even if a
future change widens run-token scope.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Annotated

import structlog
from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.bearer_auth import ApiPrincipal
from backend.api.deps import get_current_principal, get_db_session
from backend.data.rls import set_workspace_guc
from backend.data.scoping import set_current_workspace_id
from backend.identity.service import get_user_by_supabase_id, resolve_workspace_id

logger = structlog.get_logger(__name__)

#: Scope an access token must carry to mint or revoke a PAT.
PAT_ADMIN_SCOPE = "mcp:admin"


@dataclass(frozen=True)
class PatPrincipal:
    """Who is managing personal access tokens, and in which workspace."""

    user_id: uuid.UUID
    workspace_id: uuid.UUID
    auth_kind: str  # "session_jwt" | "mcp_access_token"


async def resolve_pat_principal(
    request: Request,
    principal: Annotated[ApiPrincipal, Depends(get_current_principal)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> PatPrincipal:
    """Resolve the caller of a PAT endpoint, and hold them to ``mcp:admin``.

    The credential classes and their shared checks (signature, row revocation
    and expiry, run-scope refusal) are :mod:`backend.api.bearer_auth`'s job.
    Only the escalation rule is decided here — and it applies to reads too, so
    it cannot lean on the method-shaped scope gate.
    """
    # The access-token class is exactly the one that names its own tenant, so
    # these two fields being set IS the branch — no second source of truth.
    if principal.user_row_id is not None and principal.workspace_id is not None:
        if PAT_ADMIN_SCOPE not in principal.scopes:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"managing personal access tokens requires the {PAT_ADMIN_SCOPE} scope",
            )
        resolved = PatPrincipal(
            user_id=principal.user_row_id,
            workspace_id=principal.workspace_id,
            auth_kind="mcp_access_token",
        )
    else:
        row = await get_user_by_supabase_id(session, principal.user.id)
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="no user record for principal"
            )
        workspace_id = await resolve_workspace_id(session, supabase_user_id=principal.user.id)
        if workspace_id is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="no workspace membership for principal",
            )
        resolved = PatPrincipal(user_id=row.id, workspace_id=workspace_id, auth_kind="session_jwt")

    # Same publication `get_workspace_id` performs: the ORM auto-filter reads
    # the contextvar and Postgres RLS reads the GUC. Skipping either would make
    # these routes the one place where workspace isolation is advisory.
    set_current_workspace_id(resolved.workspace_id)
    await set_workspace_guc(await session.connection(), resolved.workspace_id)
    return resolved


__all__ = ["PAT_ADMIN_SCOPE", "PatPrincipal", "resolve_pat_principal"]
