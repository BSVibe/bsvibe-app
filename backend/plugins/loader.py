"""PluginLoader — scans a plugins directory and imports each plugin module.

Each ``<plugins_dir>/<name>/plugin.py`` should define a single
``backend.plugins.PluginBuilder`` via the ``plugin(...)`` factory and the
capability decorators. The loader picks up the builder by attribute scan
and stores the resulting :class:`PluginMeta` in the registry.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import structlog

from backend.plugins.base import PluginLoadError, PluginMeta
from backend.plugins.decorator import PluginBuilder

logger = structlog.get_logger(__name__)


class PluginLoader:
    def __init__(
        self,
        plugins_dir: Path,
        danger_analyzer: Any | None = None,
    ) -> None:
        self._plugins_dir = plugins_dir
        self._danger_analyzer = danger_analyzer
        self._registry: dict[str, PluginMeta] = {}
        self.danger_map: dict[str, bool] = {}

    async def load_all(self) -> dict[str, PluginMeta]:
        self._registry.clear()
        self.danger_map.clear()

        if not self._plugins_dir.is_dir():
            logger.warning("plugins_dir_missing", path=str(self._plugins_dir))
            return self._registry

        for entry in sorted(self._plugins_dir.iterdir()):
            if not entry.is_dir():
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
            if not entry.is_dir():
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

        if self._danger_analyzer is not None:
            code = plugin_py.read_text(encoding="utf-8")  # noqa: ASYNC240
            is_dangerous, reason = await self._danger_analyzer.analyze(
                meta.name, code, meta.description
            )
            self.danger_map[meta.name] = bool(is_dangerous)
            logger.info(
                "plugin_danger_assessed",
                name=meta.name,
                is_dangerous=is_dangerous,
                reason=reason,
            )
        else:
            self.danger_map[meta.name] = False

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
