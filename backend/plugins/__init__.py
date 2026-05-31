"""BSVibe plugin framework — capability decorator model (Workflow §6 #4).

A plugin is a single object that declares zero or more capabilities via
decorators::

    p = plugin(name="github", credentials=[...], data_jurisdiction="us")

    @p.inbound(trigger={"type": "webhook"})
    async def on_webhook(context, payload): ...

    @p.outbound(artifact_types=["pr"])
    async def deliver_pr(context, event): ...

    @p.action(name="open_pr", mcp_exposed=True)
    async def open_pr(context, branch, title, body): ...

    @p.setup
    async def setup(cred_store): ...

The old ``category=input|process|output`` field and the same-channel
``@execute.notify`` assumption are gone — outbound routing is by
artifact_type, not by inbound channel, so one plugin can deliver to many
others independently.
"""

from __future__ import annotations

from backend.plugins.base import (
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
from backend.plugins.context import (
    ChatInterface,
    KnowledgeBackend,
    LLMClient,
    NotificationInterface,
    RetrieverInterface,
    SkillContext,
)
from backend.plugins.decorator import PluginBuilder, plugin
from backend.plugins.loader import PluginLoader
from backend.plugins.runner import PluginRunner

__all__ = [
    "ActionCapability",
    "ChatInterface",
    "CompensateCapability",
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
    "SkillContext",
    "VALID_COMPENSATION_TIERS",
    "VALID_JURISDICTIONS",
    "VALID_TRIGGER_TYPES",
    "plugin",
]
