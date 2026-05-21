"""Dataclasses + pydantic models for preset templates.

Presets are **code-defined** templates (see :class:`PresetRegistry`),
not DB rows — applying one writes into the existing routing-rules /
intent-definitions / intent-examples tables. The audit event row gives
the post-hoc trail; there's no separate ``presets`` table.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

TargetLevel = Literal["economy", "balanced", "premium"]


@dataclass(frozen=True)
class PresetCondition:
    """One AND-clause within a preset rule. Resolved to ``RuleCondition``
    at apply time."""

    condition_type: str
    field: str
    operator: str
    value: Any


@dataclass
class PresetRule:
    """A routing rule shipped with a preset. ``target_level`` is resolved
    to a concrete model via :class:`ModelMapping` at apply time."""

    name: str
    target_level: TargetLevel
    is_default: bool = False
    conditions: list[PresetCondition] = field(default_factory=list)


@dataclass
class PresetIntent:
    """An intent definition shipped with a preset."""

    name: str
    description: str = ""
    examples: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class PresetTemplate:
    """Full preset — intents + rules. ``name`` is the lookup key."""

    name: str
    description: str
    intents: tuple[PresetIntent, ...] = ()
    rules: tuple[PresetRule, ...] = ()


class ModelMapping(BaseModel):
    """Maps abstract target levels to concrete model names the account
    has registered in its ``model_catalog_entries``."""

    model_config = ConfigDict(extra="forbid")

    economy: str = Field(..., min_length=1)
    balanced: str = Field(..., min_length=1)
    premium: str = Field(..., min_length=1)

    def resolve(self, level: str) -> str:
        # Unknown level falls back to ``balanced`` — keeps a typo or a
        # rule with an unexpected level still routable.
        return {
            "economy": self.economy,
            "balanced": self.balanced,
            "premium": self.premium,
        }.get(level, self.balanced)


class PresetApplyRequest(BaseModel):
    """REST payload for ``POST /admin/presets/{name}/apply`` (wired in Bundle 2)."""

    model_config = ConfigDict(extra="forbid")

    preset_name: str = Field(..., min_length=1)
    model_mapping: ModelMapping


@dataclass(frozen=True)
class PresetApplyResult:
    preset_name: str
    rules_created: int
    intents_created: int
    examples_created: int
