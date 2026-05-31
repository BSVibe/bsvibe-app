"""Presets (Bundle 1.5e) — code-defined templates of rules + intents."""

from backend.router.presets.events import PresetAppliedEvent
from backend.router.presets.models import (
    ModelMapping,
    PresetApplyRequest,
    PresetApplyResult,
    PresetCondition,
    PresetIntent,
    PresetRule,
    PresetTemplate,
)
from backend.router.presets.registry import PresetRegistry, get_builtin_presets
from backend.router.presets.service import AuditEmitterProtocol, PresetService

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
