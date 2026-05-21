"""Graph sub-package — vault, graph backend, writer, sync, analytics, storage.

Lifted from `bsage.garden.*` in Phase 1 monorepo bundle. The
:class:`RestrictedPluginGarden` wrapper lives here (re-exported) because it
guards the knowledge write boundary — plugins and MCP callers receive this
wrapper instead of the raw :class:`GardenWriter`.
"""

from __future__ import annotations

from backend.knowledge.graph.restricted import RestrictedPluginGarden

__all__ = ["RestrictedPluginGarden"]
