"""/api/v1/intents — list intent definitions for the current (workspace, account)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import get_db_session, get_workspace_id, require_account_id
from backend.gateway.embedding.repository import IntentRepository

router = APIRouter()


class IntentResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    name: str
    description: str | None = None
    threshold: float | None = None


@router.get("")
async def list_intents(
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    account_id: Annotated[uuid.UUID, Depends(require_account_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> list[IntentResponse]:
    """List intent definitions for this workspace + account."""
    repo = IntentRepository(session)
    rows = await repo.list_intents(workspace_id=workspace_id, account_id=account_id)
    return [
        IntentResponse(
            id=row.id,
            name=row.name,
            description=getattr(row, "description", None),
            threshold=getattr(row, "threshold", None),
        )
        for row in rows
    ]
