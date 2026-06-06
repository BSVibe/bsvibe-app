"""/api/v1/run-routing — author per-workspace RUN routing rules (Lift E2).

These rules pick which ModelAccount handles a run, keyed on the dispatch
caller_id + the run's framed signals. Lift E2 makes ``caller_id`` a
first-class column: every non-default rule must declare which caller
it routes, validated against
:mod:`backend.dispatch.caller_registry`. Default (catch-all) rules are
the only ones that may omit ``caller_id``.

Distinct from ``/api/v1/rules`` (the legacy LiteLLM-hook model rules;
unchanged by this lift).

Write-time validation:

* ``caller_id`` (when set) must be a known caller — either a static
  entry in :data:`backend.dispatch.caller_registry.KNOWN_CALLERS` or the
  workspace-managed ``skill.<name>`` namespace.
* ``conditions`` field/operator must be in the engine's
  :data:`ALLOWED_FIELDS` / :data:`VALID_OPERATORS`.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import get_db_session, get_workspace_id
from backend.api.v1._router_deps import get_run_routing_rule_repository
from backend.dispatch.caller_registry import KNOWN_CALLERS, SKILL_CALLER_PREFIX
from backend.router.domain.repositories import RunRoutingRuleRepository
from backend.router.routing.run_routing.db import RunRoutingRuleRow
from backend.router.routing.run_routing.engine import ALLOWED_FIELDS, VALID_OPERATORS

logger = structlog.get_logger(__name__)

router = APIRouter()


def _validate_caller_id(value: str) -> str:
    """Reject a caller_id that isn't a static known caller or a
    well-formed ``skill.<name>`` id. The skill namespace is permissive
    here (we accept any well-formed name); the resolver does the final
    spec lookup against the per-workspace skill loader at dispatch time.
    """
    if value in KNOWN_CALLERS:
        return value
    if value.startswith(SKILL_CALLER_PREFIX) and len(value) > len(SKILL_CALLER_PREFIX):
        return value
    known = ", ".join(sorted(KNOWN_CALLERS))
    raise ValueError(f"unknown caller_id {value!r}; expected one of {{{known}}} or 'skill.<name>'")


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
    # Lift E2 — required for any non-default rule; ``None`` only for the
    # catch-all default. Validated against the caller registry.
    caller_id: str | None = Field(default=None, max_length=120)
    priority: int = Field(default=0, ge=0)
    is_default: bool = False
    target: str = Field(min_length=1, max_length=255)
    conditions: list[ConditionPayload] = Field(default_factory=list)
    is_active: bool = True

    @field_validator("caller_id")
    @classmethod
    def _caller_known(cls, v: str | None) -> str | None:
        if v is None:
            return v
        return _validate_caller_id(v)

    @model_validator(mode="after")
    def _non_default_requires_caller(self) -> RunRuleCreate:
        # Non-default rules must declare a caller_id (otherwise they'd
        # match nothing through the resolver's column-first matcher).
        # Back-compat: the row may still carry an in-conditions
        # caller_id clause — accept that shape too so a founder can
        # author the convenience condition form.
        if self.is_default:
            return self
        if self.caller_id:
            return self
        condition_callers = [
            c
            for c in self.conditions
            if c.field == "caller_id" and c.operator == "eq" and isinstance(c.value, str)
        ]
        if not condition_callers:
            raise ValueError(
                "non-default run-routing rules must declare a caller_id "
                "(either the top-level field or a {field:'caller_id', operator:'eq'} condition)"
            )
        return self


class RunRuleResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    workspace_id: uuid.UUID
    name: str
    caller_id: str | None = None
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
        caller_id=row.caller_id,
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
    rules: Annotated[RunRoutingRuleRepository, Depends(get_run_routing_rule_repository)],
) -> list[RunRuleResponse]:
    rows = await rules.list_by_workspace(workspace_id=workspace_id)
    return [_to_response(r) for r in rows]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_run_rule(
    payload: RunRuleCreate,
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    rules: Annotated[RunRoutingRuleRepository, Depends(get_run_routing_rule_repository)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> RunRuleResponse:
    row = RunRoutingRuleRow(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        name=payload.name,
        caller_id=payload.caller_id,
        priority=payload.priority,
        is_default=payload.is_default,
        target=payload.target,
        conditions=[c.model_dump() for c in payload.conditions],
        is_active=payload.is_active,
    )
    try:
        await rules.add(row)
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"a run-routing rule named {payload.name!r} already exists",
        ) from None
    logger.info(
        "run_routing_rule_created",
        workspace_id=str(workspace_id),
        name=payload.name,
        caller_id=payload.caller_id,
    )
    return _to_response(row)


@router.delete("/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_run_rule(
    rule_id: uuid.UUID,
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    rules: Annotated[RunRoutingRuleRepository, Depends(get_run_routing_rule_repository)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> None:
    row = await rules.get(workspace_id=workspace_id, rule_id=rule_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"rule {rule_id} not found"
        )
    await rules.delete(row)
    await session.commit()
