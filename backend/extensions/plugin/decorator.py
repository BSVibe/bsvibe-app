"""Engine-side re-export of the SDK's canonical ``plugin``/``PluginBuilder``.

Lift R2b unification: the engine no longer owns a distinct
``PluginBuilder`` class. The author-facing surface lives in
:mod:`bsvibe_sdk.plugin` so connectors import once from ``bsvibe_sdk``
and the engine PluginLoader's ``isinstance`` check resolves the exact
same class. This module remains for back-compat with internal callers
that still write ``from backend.extensions.plugin.decorator import plugin``.
"""

from __future__ import annotations

from bsvibe_sdk.plugin import PluginBuilder, plugin

__all__ = ["PluginBuilder", "plugin"]
