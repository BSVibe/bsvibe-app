"""Product-suppressed connector names — the data behind ``user_connectable``.

A pure-data leaf: which connectors are fully built but deliberately NOT
surfaced to users yet (a product decision, founder 2026-07-18). Extracted from
:mod:`backend.connectors.catalog` so leaf contexts (e.g.
:mod:`backend.notifications`) can honour the same suppression without importing
the plugin-loader-backed catalog (which would pull the plugin/workflow graph
across a leaf import boundary). ``catalog`` re-exports this as the single source
of truth; ``ConnectorInfo.user_connectable`` is exactly ``name not in`` this set.
"""

from __future__ import annotations

HIDDEN_CONNECTORS: frozenset[str] = frozenset({"linear", "trello"})

__all__ = ["HIDDEN_CONNECTORS"]
