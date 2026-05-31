"""Engine-side aliases for the SDK's canonical plugin contract types.

Lift R2b unification: the capability dataclasses, ``PluginMeta``, the
``VALID_*`` constants, and ``PluginRegistrationError`` all live in
:mod:`bsvibe_sdk.plugin` now. This module re-exports them under the
historical engine names so existing internal callers keep working
without a sweeping rename. ``PluginLoadError`` and ``PluginRunError``
remain engine-only (they are runtime concerns the SDK does not model).
"""

from __future__ import annotations

from bsvibe_sdk.plugin import (
    VALID_COMPENSATION_TIERS,
    VALID_JURISDICTIONS,
    VALID_TRIGGER_TYPES,
    ActionCapability,
    CompensateCapability,
    InboundCapability,
    OutboundCapability,
    PluginMeta,
    PluginRegistrationError,
)


class PluginLoadError(RuntimeError):
    """Raised when the loader cannot discover or import a plugin module."""


class PluginRunError(RuntimeError):
    """Raised when a runtime dispatch fails (missing capability, exec error, etc.)."""


__all__ = [
    "VALID_COMPENSATION_TIERS",
    "VALID_JURISDICTIONS",
    "VALID_TRIGGER_TYPES",
    "ActionCapability",
    "CompensateCapability",
    "InboundCapability",
    "OutboundCapability",
    "PluginLoadError",
    "PluginMeta",
    "PluginRegistrationError",
    "PluginRunError",
]
