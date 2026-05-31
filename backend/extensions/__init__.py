"""BSVibe Extensions context — Lift G.

Merges the former ``backend/plugins/`` capability decorator framework and
``backend/skills/`` skill-manifest engine into a single bounded context,
plus relocates ``backend/supervisor/audit/`` underneath as the first
concrete extension implementation.

Layout::

    backend/extensions/
        domain/protocols.py        — published Protocol surface (Lift G)
        plugin/                    — capability decorator framework
        skill/                     — Markdown-manifest skill engine
        implementations/
            audit/                 — first concrete extension (was supervisor/audit/)
            github/, slack/, ...   — capability-decorator plugins (no behavior change)

Top-level re-exports keep the union of the two former ``__init__.py``
surfaces so the engine names (``PluginLoader``, ``SkillLoader``, ``plugin``,
``invoke_skill``, etc.) remain importable from ``backend.extensions``.
"""

from __future__ import annotations

# NOTE: we cannot ``from backend.extensions.plugin import plugin`` here —
# the ``plugin`` decorator factory and the ``backend.extensions.plugin``
# subpackage share a name, and re-exporting the function at this level
# would shadow the subpackage attribute on ``backend.extensions`` after
# import (callers like ``import backend.extensions.plugin`` then see the
# function instead of the package). Callers want the decorator via
# ``from backend.extensions.plugin import plugin`` directly.
from backend.extensions.plugin.base import (
    VALID_COMPENSATION_TIERS,
    VALID_JURISDICTIONS,
    VALID_TRIGGER_TYPES,
    ActionCapability,
    CompensateCapability,
    InboundCapability,
    OutboundCapability,
    PluginLoadError,
    PluginMeta,
    PluginRegistrationError,
    PluginRunError,
)
from backend.extensions.plugin.context import (
    ChatInterface,
    KnowledgeBackend,
    LLMClient,
    NotificationInterface,
    RetrieverInterface,
    SkillContext,
)
from backend.extensions.plugin.decorator import PluginBuilder
from backend.extensions.plugin.loader import PluginLoader
from backend.extensions.plugin.runner import PluginRunner
from backend.extensions.skill import (
    CompletionFn,
    Searcher,
    SkillError,
    SkillLoader,
    SkillLoadError,
    SkillMeta,
    SkillRunError,
    SkillRunResult,
    invoke_skill,
)

__all__ = [
    "ActionCapability",
    "ChatInterface",
    "CompensateCapability",
    "CompletionFn",
    "InboundCapability",
    "KnowledgeBackend",
    "LLMClient",
    "NotificationInterface",
    "OutboundCapability",
    "PluginBuilder",
    "PluginLoadError",
    "PluginLoader",
    "PluginMeta",
    "PluginRegistrationError",
    "PluginRunError",
    "PluginRunner",
    "RetrieverInterface",
    "Searcher",
    "SkillContext",
    "SkillError",
    "SkillLoadError",
    "SkillLoader",
    "SkillMeta",
    "SkillRunError",
    "SkillRunResult",
    "VALID_COMPENSATION_TIERS",
    "VALID_JURISDICTIONS",
    "VALID_TRIGGER_TYPES",
    "invoke_skill",
]
