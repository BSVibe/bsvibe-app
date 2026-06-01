"""Smoke tests for the connector_dispatch package decomposition (Lift §17.7).

Lift §17.7 turns the 887-LOC ``connector_dispatch.py`` module into a
``connector_dispatch/`` package without breaking any caller. These deltas pin:

1. The public + private symbols every caller imports still resolve via the
   package facade (back-compat for tests that touch ``_NoLlm`` /
   ``_resolve_bindings`` / ``_split_summary``).
2. Each extracted helper module is importable on its own (sanity that the
   package wiring works and no circular imports were introduced).
3. The extracted file LOC budget — every helper file ≤ 400 LOC.
"""

from __future__ import annotations

from pathlib import Path

import pytest


def test_public_facade_reexports_all_symbols_used_by_callers() -> None:
    """Every name a current caller imports from
    ``backend.workflow.application.delivery.connector_dispatch`` still
    resolves after the package split.
    """
    from backend.workflow.application.delivery import connector_dispatch as cd

    # Public surface (used by app code + e2e tests + REST API + worker bootstrap).
    expected_public = {
        "OUTBOUND_EVENT_BUILDERS",
        "ConnectorDeliveryAdapter",
        "GithubBinding",
        "OutboundEventBuilder",
        "ShapedEvent",
        "build_connector_delivery_adapter",
        "build_github_workspace_provisioner",
        "build_discord_event",
        "build_email_event",
        "build_linear_event",
        "build_notion_event",
        "build_slack_event",
        "build_telegram_event",
        "build_trello_event",
        "github_remote_url",
        "resolve_github_binding",
        "run_branch_name",
    }
    # Private surface (touched by the unit test module).
    expected_private = {"_NoLlm", "_resolve_bindings", "_split_summary"}

    for name in expected_public | expected_private:
        assert hasattr(cd, name), f"connector_dispatch facade missing: {name}"


def test_each_extracted_helper_module_is_importable() -> None:
    """The new sibling helper modules import cleanly (no circular import)."""
    import importlib

    for mod in (
        "backend.workflow.application.delivery.connector_dispatch._builders",
        "backend.workflow.application.delivery.connector_dispatch._resolver",
        "backend.workflow.application.delivery.connector_dispatch._github",
        "backend.workflow.application.delivery.connector_dispatch._context",
    ):
        importlib.import_module(mod)


@pytest.mark.parametrize(
    "rel_path",
    [
        "__init__.py",
        "_builders.py",
        "_resolver.py",
        "_github.py",
        "_context.py",
    ],
)
def test_each_decomposed_file_under_400_loc(rel_path: str) -> None:
    """Lift §17.7 cap: each file in the package ≤ 400 LOC."""
    pkg_root = (
        Path(__file__).resolve().parents[2]
        / "backend/workflow/application/delivery/connector_dispatch"
    )
    path = pkg_root / rel_path
    assert path.exists(), f"missing decomposed file: {path}"
    loc = sum(1 for _ in path.open())
    assert loc <= 400, f"{rel_path} is {loc} LOC (> 400 cap)"
