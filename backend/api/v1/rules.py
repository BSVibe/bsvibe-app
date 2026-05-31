"""/api/v1/rules — founder-facing CRUD for routing rules.

The ROUTING surface in Settings → Models: list / create / update / delete the
rules that map a unit of work to a target model, for the current
(workspace, billing-account). Every endpoint is scoped exactly like the list
route — ``get_workspace_id`` + ``require_account_id`` — so a rule that belongs
to another workspace or account is invisible (404 on patch / delete, never
returned by list).

These endpoints only ADD a read/write API over the EXISTING
:class:`~backend.router.rules.repository.RulesRepository`. They do NOT touch
how rules are evaluated at runtime — the :class:`RuleEngine` keeps consuming
the same rows it always has.

Condition support is intentionally minimal (the design's ROUTING block, not a
full classifier editor): a rule may be created with no conditions (a catch-all
/ default) or with simple conditions whose ``field`` must be in the evaluator's
:data:`~backend.router.rules.conditions.ALLOWED_FIELDS` whitelist — a field
outside it would never match, so we reject it at the boundary rather than
persist a dead rule. Complex multi-condition editing is deferred.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import get_db_session, get_workspace_id, require_account_id
from backend.router.rules.conditions import ALLOWED_FIELDS
from backend.router.rules.db import RoutingRuleRow
from backend.router.rules.repository import RuleDuplicateError, RulesRepository

router = APIRouter()


class ConditionPayload(BaseModel):
    """A single rule condition the founder can attach (minimal subset).

    ``field`` is validated against the evaluator's whitelist so a typo'd /
    crafted field can't persist a rule that silently never matches.
    """

    model_config = ConfigDict(extra="forbid")

    condition_type: str = Field(min_length=1, max_length=40)
    field: str = Field(min_length=1, max_length=60)
    operator: str = Field(default="eq", min_length=1, max_length=20)
    value: Any
    negate: bool = False

    @field_validator("field")
    @classmethod
    def _field_must_be_evaluable(cls, v: str) -> str:
        if v not in ALLOWED_FIELDS:
            raise ValueError(f"field {v!r} is not an evaluable condition field")
        return v


class ConditionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    condition_type: str
    field: str
    operator: str
    value: Any
    negate: bool


class RuleResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    name: str
    priority: int
    target_model: str
    is_default: bool
    is_active: bool
    conditions: list[ConditionResponse]


class RuleCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    target_model: str = Field(min_length=1, max_length=200)
    priority: int = Field(ge=1)
    is_default: bool = False
    is_active: bool = True
    conditions: list[ConditionPayload] = Field(default_factory=list)


class RuleUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    target_model: str = Field(min_length=1, max_length=200)
    priority: int = Field(ge=1)
    is_default: bool = False
    is_active: bool = True


def _to_response(row: RoutingRuleRow) -> RuleResponse:
    """Build the wire response from a row (conditions eager-loaded via selectin)."""
    return RuleResponse(
        id=row.id,
        name=row.name,
        priority=row.priority,
        target_model=row.target_model,
        is_default=row.is_default,
        is_active=row.is_active,
        conditions=[
            ConditionResponse(
                condition_type=c.condition_type,
                field=c.field,
                operator=c.operator,
                value=c.value,
                negate=c.negate,
            )
            for c in sorted(row.conditions, key=lambda c: (c.condition_type, c.field))
        ],
    )


@router.get("")
async def list_rules(
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    account_id: Annotated[uuid.UUID, Depends(require_account_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> list[RuleResponse]:
    """List routing rules for the workspace + account, priority ascending."""
    repo = RulesRepository(session)
    rows = await repo.list_rules(workspace_id=workspace_id, account_id=account_id)
    return [_to_response(row) for row in rows]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_rule(
    payload: RuleCreate,
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    account_id: Annotated[uuid.UUID, Depends(require_account_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> RuleResponse:
    """Create a routing rule (optionally with simple conditions).

    409 on a duplicate ``(workspace, account, name)`` or a priority collision —
    surfaced calmly to the UI rather than a 500.
    """
    repo = RulesRepository(session)
    try:
        row = await repo.create_rule(
            workspace_id=workspace_id,
            account_id=account_id,
            name=payload.name,
            priority=payload.priority,
            target_model=payload.target_model,
            is_active=payload.is_active,
            is_default=payload.is_default,
        )
        if payload.conditions:
            await repo.replace_conditions(
                row.id,
                [c.model_dump() for c in payload.conditions],
            )
    except RuleDuplicateError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="a rule with that name or priority already exists",
        ) from exc
    await session.commit()
    # Re-read so the eager-loaded conditions reflect any just-added children.
    fresh = await repo.get_rule(row.id, workspace_id=workspace_id, account_id=account_id)
    assert fresh is not None  # noqa: S101 — just created in this tx
    return _to_response(fresh)


@router.patch("/{rule_id}")
async def update_rule(
    rule_id: uuid.UUID,
    payload: RuleUpdate,
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    account_id: Annotated[uuid.UUID, Depends(require_account_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> RuleResponse:
    """Update a rule's name / target / priority / default / active flags.

    404 when the rule does not belong to the caller's workspace + account.
    Conditions are not edited here (deferred) — they are preserved.
    """
    repo = RulesRepository(session)
    try:
        row = await repo.update_rule(
            rule_id,
            workspace_id=workspace_id,
            account_id=account_id,
            name=payload.name,
            priority=payload.priority,
            is_default=payload.is_default,
            target_model=payload.target_model,
            is_active=payload.is_active,
        )
    except RuleDuplicateError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="a rule with that name or priority already exists",
        ) from exc
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="rule not found")
    await session.commit()
    fresh = await repo.get_rule(rule_id, workspace_id=workspace_id, account_id=account_id)
    assert fresh is not None  # noqa: S101 — just updated in this tx
    return _to_response(fresh)


@router.delete("/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_rule(
    rule_id: uuid.UUID,
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    account_id: Annotated[uuid.UUID, Depends(require_account_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> None:
    """Delete a rule. 404 when it is outside the caller's workspace + account."""
    repo = RulesRepository(session)
    deleted = await repo.delete_rule(rule_id, workspace_id=workspace_id, account_id=account_id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="rule not found")
    await session.commit()
