"""Lift C — post-fold import-surface smoke test.

Asserts the mechanical end state of Lift C:

1. The top-level ``backend.routing`` package no longer exists; importing it
   raises ``ModuleNotFoundError``. Likewise for its sub-modules
   (``backend.routing.engine``, ``backend.routing.db``,
   ``backend.routing.multi_account``, ``backend.routing.tier_default``).
2. The run-routing engine has moved under
   ``backend.router.routing.run_routing`` and all previously exported names
   are reachable from the new location.
3. The pre-existing ``backend.router.routing`` LLM-provider routing surface
   (Bundle 1.5b — catalog/logs/registry/strategies) is preserved by Lift C.

If any of these fail, the fold + import migration is incomplete.
"""

from __future__ import annotations

import importlib

import pytest

# Built from string literals so a blanket sed in a future lift cannot silently
# rewrite the gone-package names — the whole point of these tests.
_OLD_ROUTING = "backend." + "routing"


def test_old_top_level_routing_package_is_gone() -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(_OLD_ROUTING)


@pytest.mark.parametrize(
    "submodule",
    ["engine", "db", "multi_account", "tier_default"],
)
def test_old_top_level_routing_submodules_are_gone(submodule: str) -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(f"{_OLD_ROUTING}.{submodule}")


def test_new_run_routing_package_importable() -> None:
    mod = importlib.import_module("backend.router.routing.run_routing")
    expected = {
        "ALLOWED_FIELDS",
        "RoutingContext",
        "RunRoutingRuleRow",
        "evaluate_rules",
        "resolve_route",
    }
    missing = expected - set(dir(mod))
    assert not missing, f"backend.router.routing.run_routing missing exports: {missing}"


def test_new_run_routing_submodules_importable() -> None:
    db = importlib.import_module("backend.router.routing.run_routing.db")
    assert hasattr(db, "RunRoutingRuleRow")
    engine = importlib.import_module("backend.router.routing.run_routing.engine")
    assert hasattr(engine, "resolve_route")
    assert hasattr(engine, "RoutingContext")
    assert hasattr(engine, "ALLOWED_FIELDS")
    assert hasattr(engine, "evaluate_rules")
    multi = importlib.import_module("backend.router.routing.run_routing.multi_account")
    assert hasattr(multi, "ROUTING_PRIORITY_KEY")
    assert hasattr(multi, "select_within_class")
    tier = importlib.import_module("backend.router.routing.run_routing.tier_default")
    assert tier is not None


def test_llm_provider_routing_surface_preserved() -> None:
    """Pre-existing backend.router.routing exports (Bundle 1.5b) survive Lift C."""
    mod = importlib.import_module("backend.router.routing")
    expected = {
        "ABTester",
        "CostOptimizer",
        "GatewayRoutingBase",
        "ModelCatalogEntryRow",
        "ModelCatalogRepository",
        "ModelRegistryService",
        "RegionSelector",
        "RoutingLogRow",
        "RoutingLogsRepository",
    }
    missing = expected - set(dir(mod))
    assert not missing, f"backend.router.routing (LLM-provider surface) missing: {missing}"
