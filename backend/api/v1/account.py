"""/api/v1/account (singular) — the caller's personal billing account.

Distinct from the plural ``/api/v1/accounts`` (ModelAccount CRUD). This thin
discovery endpoint returns the id the PWA must send as ``X-BSVibe-Account-Id``.
Create-on-read via :func:`ensure_personal_account` so a logged-in founder
always gets an id even before the bootstrap-seeded row exists.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from backend.accounts.account_service import ensure_personal_account
from backend.api.deps import get_db_session, get_workspace_id

router = APIRouter()


class AccountResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    workspace_id: uuid.UUID


@router.get("")
async def get_personal_account(
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> AccountResponse:
    """Return (create-on-read) the personal account for the active workspace."""
    account = await ensure_personal_account(session, workspace_id=workspace_id)
    await session.commit()
    return AccountResponse(id=account.id, workspace_id=account.workspace_id)
