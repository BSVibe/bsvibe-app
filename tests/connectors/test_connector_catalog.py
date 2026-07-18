"""INV-1 — connector catalog derived from ``PluginMeta`` (the single SoT).

The connector identity is historically declared in three places
(``@p.outbound`` / ``backend/connectors/kinds.py`` / the PWA mirror). INV-1
collapses that onto ``PluginMeta``. This PR is ADDITIVE: it builds the derived
catalog and proves it is lossless against the still-present hardcoded maps, so
a later PR can delete them safely.

The catalog replaces the inbound/outbound/both ``kind`` enum with three
orthogonal capability flags (``outbound`` / ``importable`` / ``webhook_trigger``)
derived from ``PluginMeta`` + the webhook registry (founder decision,
2026-07-18).
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from backend.connectors.catalog import (
    HIDDEN_CONNECTORS,
    ConnectorInfo,
    build_connector_catalog,
)
from backend.connectors.kinds import CONNECTOR_KINDS, INBOUND_IMPORT_ACTIONS
from backend.extensions.plugin.loader import PluginLoader
from backend.extensions.plugin.webhook_registry import WebhookParserRegistry
from plugin.notion import plugin as notion_module

pytestmark = pytest.mark.asyncio

# The repo-root ``plugin/`` directory holding every built-in connector.
PLUGINS_DIR = Path(notion_module.__file__).resolve().parents[1]


@pytest.fixture
async def catalog() -> dict[str, ConnectorInfo]:
    webhook_registry = WebhookParserRegistry()
    loader = PluginLoader(PLUGINS_DIR, webhook_registry=webhook_registry)
    registry = await loader.load_all()
    return build_connector_catalog(registry, webhook_registry)


async def _loaded() -> tuple[dict, WebhookParserRegistry]:
    webhook_registry = WebhookParserRegistry()
    loader = PluginLoader(PLUGINS_DIR, webhook_registry=webhook_registry)
    registry = await loader.load_all()
    return registry, webhook_registry


# --------------------------------------------------------------------------- #
# Coverage — one entry per loaded plugin, no missing, no phantom.              #
# --------------------------------------------------------------------------- #


async def test_one_entry_per_loaded_plugin(catalog: dict[str, ConnectorInfo]) -> None:
    registry, _ = await _loaded()
    assert set(catalog) == set(registry), (
        "catalog must have exactly one entry per loaded plugin — "
        f"missing={set(registry) - set(catalog)}, phantom={set(catalog) - set(registry)}"
    )


async def test_entry_name_matches_key(catalog: dict[str, ConnectorInfo]) -> None:
    for name, info in catalog.items():
        assert info.name == name


async def test_connector_info_is_frozen(catalog: dict[str, ConnectorInfo]) -> None:
    info = next(iter(catalog.values()))
    assert dataclasses.is_dataclass(info)
    with pytest.raises(dataclasses.FrozenInstanceError):
        info.outbound = True  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# Lossless-vs-hardcoded — safety proof for the future deletion of kinds.py.    #
# --------------------------------------------------------------------------- #


async def test_every_kind_map_key_resolves(catalog: dict[str, ConnectorInfo]) -> None:
    """Every ``CONNECTOR_KINDS`` key resolves to a catalog entry (no loss)."""
    for name in CONNECTOR_KINDS:
        assert name in catalog, f"kind-map connector {name!r} missing from catalog"


async def test_every_outbound_plugin_flagged(catalog: dict[str, ConnectorInfo]) -> None:
    """Every plugin declaring ``@p.outbound`` has ``outbound=True``."""
    registry, _ = await _loaded()
    for name, meta in registry.items():
        if meta.outbounds:
            assert catalog[name].outbound is True, f"{name} has @p.outbound but outbound=False"


async def test_import_actions_lossless(catalog: dict[str, ConnectorInfo]) -> None:
    """Every ``INBOUND_IMPORT_ACTIONS`` entry → importable + matching action name."""
    for name, action_name in INBOUND_IMPORT_ACTIONS.items():
        info = catalog[name]
        assert info.importable is True, (
            f"{name} listed in INBOUND_IMPORT_ACTIONS but not importable"
        )
        assert info.import_action == action_name, (
            f"{name} import_action {info.import_action!r} != hardcoded {action_name!r}"
        )


async def test_artifact_types_sorted_deduped(catalog: dict[str, ConnectorInfo]) -> None:
    """``artifact_types`` is the sorted-deduped union across a plugin's outbounds."""
    # notion declares artifact_types=["page", "page_image"] on its @p.outbound.
    assert catalog["notion"].artifact_types == ("page", "page_image")
    for info in catalog.values():
        assert list(info.artifact_types) == sorted(set(info.artifact_types))
        # Only outbound connectors carry artifact_types.
        if not info.outbound:
            assert info.artifact_types == ()


async def test_webhook_trigger_flag(catalog: dict[str, ConnectorInfo]) -> None:
    """``webhook_trigger`` reflects the webhook registry membership."""
    _, webhook_registry = await _loaded()
    for name, info in catalog.items():
        assert info.webhook_trigger is webhook_registry.is_known(name)
    # github registers a webhook parser and delivers via the special _github path.
    assert catalog["github"].webhook_trigger is True
    assert catalog["github"].outbound is True


# --------------------------------------------------------------------------- #
# NEW behavior where founder decisions diverge from the old maps.              #
# --------------------------------------------------------------------------- #


async def test_linear_trello_suppressed(catalog: dict[str, ConnectorInfo]) -> None:
    """NEW (2026-07-18): linear/trello exist + build outbound but are hidden.

    They are absent from the old ``CONNECTOR_KINDS`` map yet ARE in the catalog
    (their identity derives from ``@p.outbound``); the suppression is an
    explicit product decision, not a capability gap.
    """
    for name in ("linear", "trello"):
        assert name in catalog, f"{name} must have a catalog entry (derived from @p.outbound)"
        assert catalog[name].outbound is True
        assert catalog[name].user_connectable is False, f"{name} must be suppressed"
    assert HIDDEN_CONNECTORS == frozenset({"linear", "trello"})


async def test_non_hidden_connectors_are_user_connectable(
    catalog: dict[str, ConnectorInfo],
) -> None:
    for name, info in catalog.items():
        assert info.user_connectable is (name not in HIDDEN_CONNECTORS)


async def test_no_kind_enum_field(catalog: dict[str, ConnectorInfo]) -> None:
    """NEW (2026-07-18): the inbound/outbound/both ``kind`` enum is retired.

    ConnectorInfo carries the three orthogonal capability flags instead.
    """
    field_names = {f.name for f in dataclasses.fields(next(iter(catalog.values())))}
    assert "kind" not in field_names
    assert {"outbound", "importable", "webhook_trigger"} <= field_names
