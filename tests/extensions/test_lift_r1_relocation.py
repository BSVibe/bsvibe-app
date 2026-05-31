"""Lift R1 — smoke surface for the 9 connector plugins' relocation to
repo-root ``plugin/<name>/`` (v8 §13 Lift R + D38).

Asserts:
* each connector resolves at its NEW repo-root path (``plugin.<name>.plugin``
  module + a top-level ``PluginBuilder`` exported by the package).
* the OLD ``backend.extensions.implementations.<name>`` path raises
  ``ModuleNotFoundError``.
* the ``PluginLoader`` discovers all 9 plugins when pointed at the new
  repo-root ``plugin/`` directory.
* audit (R2 deferred) is intentionally still at the old path — pinned here
  so the next lift removes the marker on the same commit.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from backend.extensions.plugin.loader import PluginLoader

_CONNECTORS = (
    "github",
    "sentry",
    "slack",
    "telegram",
    "discord",
    "email_sender",
    "linear",
    "notion",
    "trello",
)

# Kept as string parts so the global rename sweep can't silently rewrite them.
_OLD = "backend." + "extensions." + "implementations"


@pytest.mark.parametrize("name", _CONNECTORS)
def test_connector_at_new_repo_root_path(name: str) -> None:
    mod = importlib.import_module(f"plugin.{name}.plugin")
    # Each connector module exports the module-level ``plugin = plugin(...)``
    # builder per the SDK pattern.
    assert hasattr(mod, "plugin"), f"plugin.{name}.plugin missing 'plugin' builder"


@pytest.mark.parametrize("name", _CONNECTORS)
def test_connector_old_path_gone(name: str) -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(f"{_OLD}.{name}")


def test_audit_still_at_old_path_pending_r2() -> None:
    # R1 ships 9 connectors only. Audit relocation needs EventBus rewire of
    # ~28 backend emission sites (see PR body). Keep this assertion green
    # until R2 lands so the marker test forces R2 to delete it.
    audit_mod = importlib.import_module(f"{_OLD}.audit")
    assert hasattr(audit_mod, "safe_emit")


def test_plugin_loader_discovers_all_at_new_path() -> None:
    # repo-root plugin/ resolves relative to backend's parent. Mirrors the
    # discovery path the production loader uses post-R1.
    repo_root = Path(__file__).resolve().parents[2]
    plugin_dir = repo_root / "plugin"
    assert plugin_dir.is_dir(), f"expected repo-root plugin/ at {plugin_dir}"

    async def _go() -> dict[str, object]:
        loader = PluginLoader(plugin_dir)
        return await loader.load_all()

    import asyncio

    registry = asyncio.run(_go())
    discovered = sorted(registry)
    # Note: the loader keys the registry by the plugin's declared
    # ``name=`` attribute, not by directory name. ``email_sender/`` is
    # declared as ``email-sender`` (hyphen, pre-Lift R convention).
    expected = {n.replace("_", "-") if n == "email_sender" else n for n in _CONNECTORS}
    assert expected.issubset(discovered), (
        f"missing connectors: {expected - set(discovered)}; got {discovered}"
    )
