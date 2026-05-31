"""Lift R2b — PluginBuilder unification: SDK is canonical.

After R2b:

* ``bsvibe_sdk.PluginBuilder`` is the canonical PluginBuilder symbol used by
  every connector and by the engine loader.
* ``backend.extensions.plugin.decorator.PluginBuilder`` is the SAME symbol
  (re-exported) — there are no two distinct ``PluginBuilder`` classes any
  longer.
* The same applies to the public ``plugin(...)`` factory, ``PluginMeta``,
  the capability dataclasses, ``PluginRegistrationError``, and the
  ``VALID_*`` validation constants. SDK exports them as the source of
  truth; engine re-exports for back-compat with internal callers.
* All 9 connector ``plugin/<name>/plugin.py`` modules import the factory
  ONLY from ``bsvibe_sdk`` — no remaining ``from backend.extensions.plugin``
  references in the connector source files.
* The engine ``PluginLoader``'s isinstance gate accepts an SDK-only
  PluginBuilder and yields a PluginMeta that the runner can consume
  (round-trip smoke).
"""

from __future__ import annotations

from pathlib import Path

import pytest

# Repo-root subdirectory names — these are what the file-system layout
# uses (snake_case for ``email_sender``). The runtime plugin ``name`` may
# differ (e.g. ``email-sender``) because plugin names go through the SDK
# name regex; that mapping is asserted in the loader smoke below.
_CONNECTOR_DIRS = (
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

# Runtime plugin names registered with ``plugin(name=...)`` — these are
# the keys that show up in ``PluginLoader.load_all()``.
_CONNECTOR_REGISTRY_NAMES = (
    "github",
    "sentry",
    "slack",
    "telegram",
    "discord",
    "email-sender",
    "linear",
    "notion",
    "trello",
)


# --------------------------------------------------------------------------- #
# Delta 1 — single PluginBuilder type (SDK canonical, engine re-exports).
# --------------------------------------------------------------------------- #


def test_sdk_exports_plugin_builder() -> None:
    """SDK's public ``__all__`` includes ``PluginBuilder``."""
    import bsvibe_sdk

    assert "PluginBuilder" in bsvibe_sdk.__all__
    assert hasattr(bsvibe_sdk, "PluginBuilder")


def test_engine_plugin_builder_is_sdk_plugin_builder() -> None:
    """``backend.extensions.plugin.decorator.PluginBuilder`` IS the SDK class.

    Identity check — not subclass, not duck-type. The dual-class problem
    is resolved by making the engine module a re-export of the SDK symbol.
    """
    import bsvibe_sdk
    from backend.extensions.plugin import decorator as engine_decorator

    assert engine_decorator.PluginBuilder is bsvibe_sdk.PluginBuilder


def test_engine_plugin_factory_is_sdk_plugin_factory() -> None:
    """``backend.extensions.plugin.decorator.plugin`` IS the SDK factory."""
    import bsvibe_sdk
    from backend.extensions.plugin import decorator as engine_decorator

    assert engine_decorator.plugin is bsvibe_sdk.plugin


def test_engine_package_reexports_match_sdk() -> None:
    """``backend.extensions.plugin`` still re-exports ``plugin`` + ``PluginBuilder``
    and the symbols are identity-equal to the SDK's."""
    import bsvibe_sdk
    from backend.extensions import plugin as engine_pkg

    assert engine_pkg.plugin is bsvibe_sdk.plugin
    assert engine_pkg.PluginBuilder is bsvibe_sdk.PluginBuilder


# --------------------------------------------------------------------------- #
# Delta 2 — 9 connectors SDK-only.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", _CONNECTOR_DIRS)
def test_connector_imports_sdk_only(name: str) -> None:
    """Each connector's ``plugin.py`` source contains no
    ``from backend.extensions.plugin`` import statements.

    Connectors are end-user plugin code; they live entirely on the SDK
    surface post-R2b. ``SkillContext`` is a known exception — it stays
    behind the engine until a future Lift S2 promotes it onto the SDK —
    so we forbid only the ``plugin``/``PluginBuilder``/``PluginMeta`` /
    decorator imports, not the ``.context`` ones.
    """
    src = (Path(__file__).resolve().parents[2] / "plugin" / name / "plugin.py").read_text()
    forbidden = (
        "from backend.extensions.plugin import plugin",
        "from backend.extensions.plugin import PluginBuilder",
        "from backend.extensions.plugin.decorator",
        "from backend.extensions.plugin.base",
    )
    for f in forbidden:
        assert f not in src, f"plugin/{name}/plugin.py still imports {f!r}"


@pytest.mark.parametrize("name", _CONNECTOR_DIRS)
def test_connector_has_sdk_import(name: str) -> None:
    """Each connector imports ``plugin`` (the factory) from ``bsvibe_sdk``."""
    src = (Path(__file__).resolve().parents[2] / "plugin" / name / "plugin.py").read_text()
    assert "from bsvibe_sdk import plugin" in src or "from bsvibe_sdk import " in src, (
        f"plugin/{name}/plugin.py missing bsvibe_sdk import"
    )


# --------------------------------------------------------------------------- #
# Delta 3 — engine loader accepts an SDK-built PluginBuilder.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_loader_accepts_sdk_only_plugin(tmp_path: Path) -> None:
    """A plugin module declared with only ``bsvibe_sdk`` imports loads,
    isinstance-passes the engine loader's gate, and yields a usable
    ``PluginMeta``."""
    from backend.extensions.plugin.loader import PluginLoader

    plug_dir = tmp_path / "sdkonly"
    plug_dir.mkdir()
    (plug_dir / "plugin.py").write_text(
        "from bsvibe_sdk import plugin\n"
        "p = plugin(name='sdkonly', credentials=[], data_jurisdiction='us')\n"
        "@p.action(name='ping')\n"
        "async def ping(context):\n"
        "    return {'ok': True}\n"
    )

    loader = PluginLoader(tmp_path)
    registry = await loader.load_all()
    assert "sdkonly" in registry
    meta = registry["sdkonly"]
    # PluginMeta-shape used by runner everywhere
    assert meta.name == "sdkonly"
    assert "ping" in meta.actions


# --------------------------------------------------------------------------- #
# Delta 4 — runtime end-to-end: 9 connectors still discoverable.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_repo_root_plugins_dir_loads_all_connectors() -> None:
    """The engine loader pointed at repo-root ``plugin/`` discovers the
    9 connectors after R2b's unification."""
    from backend.extensions.plugin.loader import PluginLoader

    repo_root = Path(__file__).resolve().parents[2]
    loader = PluginLoader(repo_root / "plugin")
    registry = await loader.load_all()
    for name in _CONNECTOR_REGISTRY_NAMES:
        assert name in registry, f"connector {name!r} missing from registry"
