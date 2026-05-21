"""Presets (Bundle 1.5e) — code-defined templates of rules + intents."""

from backend.gateway.presets.events import PresetAppliedEvent
from backend.gateway.presets.models import (
    ModelMapping,
    PresetApplyRequest,
    PresetApplyResult,
    PresetCondition,
    PresetIntent,
    PresetRule,
    PresetTemplate,
)
from backend.gateway.presets.registry import PresetRegistry, get_builtin_presets
from backend.gateway.presets.service import AuditEmitterProtocol, PresetService

__all__ = [
    "AuditEmitterProtocol",
    "ModelMapping",
    "PresetAppliedEvent",
    "PresetApplyRequest",
    "PresetApplyResult",
    "PresetCondition",
    "PresetIntent",
    "PresetRegistry",
    "PresetRule",
    "PresetService",
    "PresetTemplate",
    "get_builtin_presets",
]
