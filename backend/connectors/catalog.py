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

INV-1 cutover — this module is now the SOLE source of truth. The hardcoded
``backend/connectors/kinds.py`` maps are deleted; the REST validator, the
import-action resolution, and the founder-facing capability flags all read
:func:`get_connector_catalog`.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

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


# --------------------------------------------------------------------------- #
# Process-wide cached accessor.                                                #
#                                                                              #
# The catalog is derived from the repo-root ``plugin/`` tree, which is static  #
# for a running process, so it is built ONCE and cached. Every caller (the     #
# REST create-validator, the ``/connectors/catalog`` endpoint, the import gate,#
# the MCP tools) reads through :func:`get_connector_catalog` — nobody          #
# re-derives it. The accessor builds a FRESH webhook registry alongside the    #
# plugin registry so the ``webhook_trigger`` flag is self-contained and does   #
# not depend on when the process-wide default registry was populated.          #
#                                                                              #
# Tests that need a pristine build call :func:`reset_connector_catalog`.       #
# --------------------------------------------------------------------------- #

# ``backend/connectors/catalog.py`` → parents[2] is the repo root.
_PLUGINS_DIR = Path(__file__).resolve().parents[2] / "plugin"


@lru_cache(maxsize=1)
def get_connector_catalog() -> dict[str, ConnectorInfo]:
    """Return the cached, process-wide derived catalog (built on first use)."""
    # Local import keeps the loader dependency lazy (mirrors the other
    # request-time loader touch-points) and avoids an import cycle.
    from backend.extensions.plugin.loader import PluginLoader  # noqa: PLC0415

    webhook_registry = WebhookParserRegistry()
    loader = PluginLoader(_PLUGINS_DIR, webhook_registry=webhook_registry)
    registry = loader.load_all_sync()
    return build_connector_catalog(registry, webhook_registry)


def reset_connector_catalog() -> None:
    """Drop the cached catalog so the next access rebuilds it. Test-only."""
    get_connector_catalog.cache_clear()


def legacy_kind(info: ConnectorInfo) -> str:
    """Derive the retired inbound/outbound/both ``kind`` from capability flags.

    Backward-compat for the pre-catalog PWA (which reads ``connector.kind`` on
    the connector ROW to decide whether to show the "Import now" button);
    removed once PR-8 migrates the connector-row UI to the capability flags
    (INV-1 expand/contract). Derived from the flags — the deleted ``kinds.py``
    map is NOT reintroduced.
    """
    if info.importable and info.outbound:
        return "both"
    if info.importable:
        return "inbound"
    return "outbound"


__all__ = [
    "HIDDEN_CONNECTORS",
    "ConnectorInfo",
    "build_connector_catalog",
    "get_connector_catalog",
    "legacy_kind",
    "reset_connector_catalog",
]
