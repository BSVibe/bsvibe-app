"""PluginLoader — scans a plugins directory and imports each plugin module.

Each ``<plugins_dir>/<name>/plugin.py`` should define a single
``backend.extensions.plugin.PluginBuilder`` via the ``plugin(...)`` factory and the
capability decorators. The loader picks up the builder by attribute scan
and stores the resulting :class:`PluginMeta` in the registry.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import structlog

from backend.extensions.plugin.base import PluginLoadError, PluginMeta
from backend.extensions.plugin.decorator import PluginBuilder

logger = structlog.get_logger(__name__)


class PluginLoader:
    def __init__(
        self,
        plugins_dir: Path,
    ) -> None:
        self._plugins_dir = plugins_dir
        self._registry: dict[str, PluginMeta] = {}

    async def load_all(self) -> dict[str, PluginMeta]:
        self._registry.clear()

        if not self._plugins_dir.is_dir():
            logger.warning("plugins_dir_missing", path=str(self._plugins_dir))
            return self._registry

        for entry in sorted(self._plugins_dir.iterdir()):
            if not self._is_candidate_dir(entry):
                continue
            plugin_py = entry / "plugin.py"
            if not plugin_py.exists():
                logger.warning("plugin_missing_file", path=str(entry))
                continue
            await self._load_one(plugin_py)
        return self._registry

    async def scan_new(self) -> dict[str, PluginMeta]:
        """Load only entries not already present in the registry."""
        new_entries: dict[str, PluginMeta] = {}
        if not self._plugins_dir.is_dir():
            return new_entries

        for entry in sorted(self._plugins_dir.iterdir()):
            if not self._is_candidate_dir(entry):
                continue
            plugin_py = entry / "plugin.py"
            if not plugin_py.exists():
                continue
            if entry.name in self._registry:
                continue
            meta = await self._load_one(plugin_py)
            if meta is not None and meta.name not in new_entries:
                new_entries[meta.name] = meta
        return new_entries

    @staticmethod
    def _is_candidate_dir(entry: Path) -> bool:
        """Filter scan noise: dotfiles, ``__pycache__``, and other dunder dirs.

        Lift R1 moves plugins to repo-root ``plugin/<name>/``. Python may
        write ``__pycache__/`` alongside them and pytest's discovery can
        drop ``.pytest_cache``; neither is a plugin and emitting a
        ``plugin_missing_file`` warning for each is noisy.
        """
        if not entry.is_dir():
            return False
        name = entry.name
        return not (name.startswith(".") or name.startswith("__"))

    def get(self, name: str) -> PluginMeta:
        if name not in self._registry:
            raise PluginLoadError(f"Plugin {name!r} not found in registry")
        return self._registry[name]

    async def _load_one(self, plugin_py: Path) -> PluginMeta | None:
        try:
            meta = self._import_plugin(plugin_py)
        except Exception as exc:
            logger.warning("plugin_load_failed", path=str(plugin_py), error=str(exc))
            return None

        self._registry[meta.name] = meta
        logger.info("plugin_loaded", name=meta.name)
        return meta

    @staticmethod
    def _import_plugin(path: Path) -> PluginMeta:
        spec = importlib.util.spec_from_file_location(f"_bsvibe_plugin_{path.parent.name}", path)
        if spec is None or spec.loader is None:
            raise PluginLoadError(f"Cannot load plugin module: {path}")

        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        for attr_name in dir(module):
            obj = getattr(module, attr_name)
            if isinstance(obj, PluginBuilder):
                return obj.meta

        raise PluginLoadError(f"No plugin(...) declaration found in {path}")
