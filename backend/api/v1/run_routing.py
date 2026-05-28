"""/api/v1/run-routing — author the per-workspace RUN routing rules (P1-L1b).

These rules pick WHICH ModelAccount (native vs executor CLI) drives a run,
keyed on the run's framed signals (see :mod:`backend.routing.engine`). Distinct
from ``/api/v1/rules`` (the gateway's account-scoped chat/model routing). An
admin/infra surface — rule-less workspaces keep the legacy single-active
behaviour, so creating the first rule is opt-in.

Write-time validation rejects a condition whose ``field`` isn't in the engine's
``ALLOWED_FIELDS`` or whose ``operator`` isn't a real operator — a typo would
otherwise persist as a rule that silently never matches.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import get_db_session, get_workspace_id
from backend.routing.db import RunRoutingRuleRow
from backend.routing.engine import ALLOWED_FIELDS, VALID_OPERATORS

logger = structlog.get_logger(__name__)

router = APIRouter()


class ConditionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: str = Field(min_length=1)
    operator: str = "eq"
    value: Any = None
    negate: bool = False

    @field_validator("field")
    @classmethod
    def _field_allowed(cls, v: str) -> str:
        if v not in ALLOWED_FIELDS:
            allowed = ", ".join(sorted(ALLOWED_FIELDS))
            raise ValueError(f"unknown condition field {v!r}; allowed: {allowed}")
        return v

    @field_validator("operator")
    @classmethod
    def _operator_valid(cls, v: str) -> str:
        if v not in VALID_OPERATORS:
            raise ValueError(
                f"unknown operator {v!r}; allowed: {', '.join(sorted(VALID_OPERATORS))}"
            )
        return v


class RunRuleCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    priority: int = Field(default=0, ge=0)
    is_default: bool = False
    target: str = Field(min_length=1, max_length=255)
    conditions: list[ConditionPayload] = Field(default_factory=list)
    is_active: bool = True


class RunRuleResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    workspace_id: uuid.UUID
    name: str
    priority: int
    is_default: bool
    target: str
    conditions: list[dict[str, Any]]
    is_active: bool
    created_at: datetime


def _to_response(row: RunRoutingRuleRow) -> RunRuleResponse:
    return RunRuleResponse(
        id=row.id,
        workspace_id=row.workspace_id,
        name=row.name,
        priority=row.priority,
        is_default=row.is_default,
        target=row.target,
        conditions=row.conditions if isinstance(row.conditions, list) else [],
        is_active=row.is_active,
        created_at=row.created_at,
    )


@router.get("")
async def list_run_rules(
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> list[RunRuleResponse]:
    rows = (
        (
            await session.execute(
                select(RunRoutingRuleRow)
                .where(RunRoutingRuleRow.workspace_id == workspace_id)
                .order_by(RunRoutingRuleRow.priority.asc())
            )
        )
        .scalars()
        .all()
    )
    return [_to_response(r) for r in rows]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_run_rule(
    payload: RunRuleCreate,
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> RunRuleResponse:
    row = RunRoutingRuleRow(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        name=payload.name,
        priority=payload.priority,
        is_default=payload.is_default,
        target=payload.target,
        conditions=[c.model_dump() for c in payload.conditions],
        is_active=payload.is_active,
    )
    session.add(row)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"a run-routing rule named {payload.name!r} already exists",
        ) from None
    logger.info("run_routing_rule_created", workspace_id=str(workspace_id), name=payload.name)
    return _to_response(row)


@router.delete("/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_run_rule(
    rule_id: uuid.UUID,
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> None:
    row = await session.get(RunRoutingRuleRow, rule_id)
    if row is None or row.workspace_id != workspace_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"rule {rule_id} not found"
        )
    await session.delete(row)
    await session.commit()
