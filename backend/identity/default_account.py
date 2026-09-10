"""게이트 2 — "first model account becomes the workspace default".

A composition helper both the REST (``api/v1/accounts``) and MCP
(``mcp/tools/model_accounts_tools``) create paths call. Lives in a router-free
identity module so the MCP import contract (mcp → identity is allowed, mcp →
router is not) holds — ``identity.service`` imports ``router`` and so cannot be
the home.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from backend.identity.workspaces_db import WorkspaceRow


async def set_default_model_account_if_unset(
    session: AsyncSession, *, workspace_id: uuid.UUID, model_account_id: uuid.UUID
) -> bool:
    """Make ``model_account_id`` the workspace default iff none is set yet.

    The founder's FIRST model account becomes the workspace default so their
    first run resolves (resolver tier 2) instead of hard-failing
    ``no_model_account`` — the "hidden step" a new founder hit. NEVER overrides
    an existing default, so the resolver's "never auto-stamps mid-life" contract
    holds: this only fills the empty initial slot. Returns whether it set it.
    """
    ws = await session.get(WorkspaceRow, workspace_id)
    if ws is None or ws.default_account_id is not None:
        return False
    ws.default_account_id = model_account_id
    return True
