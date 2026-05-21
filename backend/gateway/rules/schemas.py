"""Pydantic in/out shapes for the rules REST surface.

Bundle 1.5a does not wire these to a router — Bundle 2 mounts them when
``backend/api/`` lands. Keeping them here so the rest API plan can
import a stable Pydantic shape.
"""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class RuleConditionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    condition_type: str = Field(min_length=1, max_length=40)
    field: str = Field(min_length=1, max_length=60)
    operator: str = Field(min_length=1, max_length=20)
    value: Any
    negate: bool = False


class RuleConditionOut(RuleConditionIn):
    id: uuid.UUID


class RoutingRuleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    priority: int = Field(ge=1)
    target_model: str = Field(min_length=1, max_length=200)
    is_active: bool = True
    is_default: bool = False
    conditions: list[RuleConditionIn] = Field(default_factory=list)


class RoutingRuleOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    workspace_id: uuid.UUID
    account_id: uuid.UUID
    name: str
    priority: int
    is_active: bool
    is_default: bool
    target_model: str
    conditions: list[RuleConditionOut] = Field(default_factory=list)


class ReorderRulesIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    priorities: dict[uuid.UUID, int]
