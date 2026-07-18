"""Connector catalog — derived from ``PluginMeta`` (INV-1 single source of truth).

Connector identity was historically declared in three places: the plugin
decorators (``@p.outbound`` / ``@p.action``), the hardcoded
``backend/connectors/kinds.py`` map, and the PWA mirror. INV-1 collapses that
onto ``PluginMeta``. This module derives the founder-visible catalog straight
from the loaded plugin registry + the webhook parser registry, so no second
place restates what a connector can do.

Capability model (founder decision, 2026-07-18)
------------------------------------------------
The old inbound / outbound / both ``kind`` enum was internally inconsistent (a
connector that both imports and delivers, or one that only receives webhooks,
did not fit three buckets). It is replaced by THREE orthogonal capability flags,
each derived from a declaration site:

* ``outbound`` — the plugin declares at least one ``@p.outbound``.
* ``importable`` — the plugin marks an ``@p.action(import_trigger=True)``.
* ``webhook_trigger`` — a ``@webhook(...)`` parser is registered for the name.

A connector may set any combination (slack is outbound + webhook_trigger; notion
is outbound + importable; obsidian is importable-only).

This PR is ADDITIVE: ``kinds.py`` and the PWA mirror still exist; a later PR
deletes them once the catalog is proven lossless against them.
"""

from __future__ import annotations

from dataclasses import dataclass

from backend.extensions.plugin.base import PluginMeta
from backend.extensions.plugin.webhook_registry import WebhookParserRegistry

# Connectors that are fully built (builders + validator accept them) but are
# deliberately NOT surfaced to users yet — a product suppression decision
# (founder, 2026-07-18), not a capability gap. To expose one, remove its name
# here; nothing else changes because the catalog derives everything else.
HIDDEN_CONNECTORS: frozenset[str] = frozenset({"linear", "trello"})


@dataclass(frozen=True, slots=True)
class ConnectorInfo:
    """The derived, founder-visible facts about one connector.

    Every field is derived from ``PluginMeta`` + the webhook registry — there is
    no hand-maintained second copy. ``kind`` (the retired inbound/outbound/both
    enum) is intentionally absent; the three capability flags replace it.
    """

    name: str
    outbound: bool
    importable: bool
    webhook_trigger: bool
    artifact_types: tuple[str, ...]
    import_action: str | None
    user_connectable: bool


def build_connector_catalog(
    registry: dict[str, PluginMeta],
    webhook_registry: WebhookParserRegistry,
) -> dict[str, ConnectorInfo]:
    """Derive one :class:`ConnectorInfo` per loaded plugin.

    ``registry`` is the loaded-plugin map (``PluginLoader.load_all()``);
    ``webhook_registry`` is the same registry the loader populated with
    ``@webhook(...)`` parsers. Exactly one entry per plugin — no phantom names,
    no missing ones.
    """
    catalog: dict[str, ConnectorInfo] = {}
    for name, meta in registry.items():
        artifact_types = tuple(sorted({t for cap in meta.outbounds for t in cap.artifact_types}))
        import_action = meta.import_action_name
        catalog[name] = ConnectorInfo(
            name=name,
            outbound=bool(meta.outbounds),
            importable=import_action is not None,
            webhook_trigger=webhook_registry.is_known(name),
            artifact_types=artifact_types,
            import_action=import_action,
            user_connectable=name not in HIDDEN_CONNECTORS,
        )
    return catalog


__all__ = [
    "HIDDEN_CONNECTORS",
    "ConnectorInfo",
    "build_connector_catalog",
]
