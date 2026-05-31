"""bsvibe_sdk — public Plugin SDK for BSVibe (Lift S + Lift R2b).

Plugin-only surface (v8 §D42): Protocols + decorators + helper types
that external plugin authors import to write a BSVibe plugin without
depending on backend internals.

Design source: ``~/Docs/BSVibe_Class_Architecture_Design_2026-05-30.md``
v8 §13 Lift S + Lift R2b + D39 + D42.

Lift R2b promotes ``PluginBuilder`` / ``PluginMeta`` / capability
dataclasses / validation constants onto the public surface. The engine's
``backend.extensions.plugin.decorator`` and ``backend.extensions.plugin.base``
re-export these symbols so that any internal caller resolves them to the
exact same objects — there is now a single ``PluginBuilder`` class in the
codebase.

Example::

    from bsvibe_sdk import Context, Result, plugin

    p = plugin(name="github", credentials=[...], data_jurisdiction="us")

    @p.action(name="open_pr", mcp_exposed=True)
    async def open_pr(context: Context, *, branch: str, title: str) -> Result:
        ...
        return Result.ok({"pr_number": 42})
"""

from __future__ import annotations

from bsvibe_sdk.action import Action, action
from bsvibe_sdk.context import Context, Result
from bsvibe_sdk.event import Event, EventBusSubscriber, on_event
from bsvibe_sdk.plugin import (
    VALID_COMPENSATION_TIERS,
    VALID_JURISDICTIONS,
    VALID_TRIGGER_TYPES,
    ActionCapability,
    CompensateCapability,
    InboundCapability,
    OutboundCapability,
    Plugin,
    PluginBuilder,
    PluginDeclarationError,
    PluginMeta,
    PluginRegistrationError,
    plugin,
)
from bsvibe_sdk.version import __version__

__all__ = [
    "VALID_COMPENSATION_TIERS",
    "VALID_JURISDICTIONS",
    "VALID_TRIGGER_TYPES",
    "Action",
    "ActionCapability",
    "CompensateCapability",
    "Context",
    "Event",
    "EventBusSubscriber",
    "InboundCapability",
    "OutboundCapability",
    "Plugin",
    "PluginBuilder",
    "PluginDeclarationError",
    "PluginMeta",
    "PluginRegistrationError",
    "Result",
    "__version__",
    "action",
    "on_event",
    "plugin",
]
