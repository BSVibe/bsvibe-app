"""/api/v1/rules — list routing rules for the current (workspace, account)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import get_db_session, get_workspace_id, require_account_id
from backend.gateway.rules.repository import RulesRepository

router = APIRouter()


class RuleResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    name: str
    priority: int
    target_model: str
    is_default: bool
    is_active: bool


@router.get("")
async def list_rules(
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    account_id: Annotated[uuid.UUID, Depends(require_account_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> list[RuleResponse]:
    """List routing rules for the workspace + account, priority ascending."""
    repo = RulesRepository(session)
    rows = await repo.list_rules(workspace_id=workspace_id, account_id=account_id)
    return [
        RuleResponse(
            id=row.id,
            name=row.name,
            priority=row.priority,
            target_model=row.target_model,
            is_default=row.is_default,
            is_active=row.is_active,
        )
        for row in rows
    ]
