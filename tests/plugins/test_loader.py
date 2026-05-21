"""Tests for backend.plugins.loader — discovery + AST-validated dynamic import."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from backend.plugins import PluginLoader, PluginLoadError


def _write_plugin(root: Path, name: str, body: str) -> Path:
    pdir = root / name
    pdir.mkdir(parents=True)
    (pdir / "plugin.py").write_text(body, encoding="utf-8")
    return pdir


_SAMPLE_GITHUB = """
from backend.plugins import plugin

p = plugin(name="github", credentials=[], data_jurisdiction="us")

@p.inbound(trigger={"type": "webhook"})
async def on_webhook(context, payload):
    return {"id": payload.get("id")}

@p.outbound(artifact_types=["pr"])
async def deliver_pr(context, event):
    return {"delivered": True}
"""


_SAMPLE_NOTION = """
from backend.plugins import plugin

p = plugin(name="notion", credentials=[], data_jurisdiction="us")

@p.outbound(artifact_types=["notion_page"])
async def deliver_page(context, event):
    return {"page_id": "abc"}
"""


_NO_PLUGIN_DECL = """
def regular_function():
    return 1
"""


class TestLoaderDiscovery:
    async def test_loads_directory_with_plugin_py(self, tmp_path: Path):
        _write_plugin(tmp_path, "github", _SAMPLE_GITHUB)
        _write_plugin(tmp_path, "notion", _SAMPLE_NOTION)

        loader = PluginLoader(plugins_dir=tmp_path, danger_analyzer=None)
        registry = await loader.load_all()

        assert set(registry.keys()) == {"github", "notion"}
        assert registry["github"].data_jurisdiction == "us"
        assert len(registry["github"].inbounds) == 1
        assert len(registry["github"].outbounds) == 1

    async def test_ignores_directories_without_plugin_py(self, tmp_path: Path):
        (tmp_path / "not_a_plugin").mkdir()
        _write_plugin(tmp_path, "github", _SAMPLE_GITHUB)

        loader = PluginLoader(plugins_dir=tmp_path, danger_analyzer=None)
        registry = await loader.load_all()

        assert set(registry.keys()) == {"github"}

    async def test_missing_plugins_dir_returns_empty(self, tmp_path: Path):
        loader = PluginLoader(plugins_dir=tmp_path / "missing", danger_analyzer=None)
        registry = await loader.load_all()
        assert registry == {}

    async def test_warns_when_no_plugin_declaration(self, tmp_path: Path):
        _write_plugin(tmp_path, "broken", _NO_PLUGIN_DECL)
        loader = PluginLoader(plugins_dir=tmp_path, danger_analyzer=None)
        registry = await loader.load_all()
        assert registry == {}

    async def test_get_raises_for_unknown_plugin(self, tmp_path: Path):
        loader = PluginLoader(plugins_dir=tmp_path, danger_analyzer=None)
        await loader.load_all()
        with pytest.raises(PluginLoadError):
            loader.get("nope")

    async def test_get_returns_registered_plugin(self, tmp_path: Path):
        _write_plugin(tmp_path, "github", _SAMPLE_GITHUB)
        loader = PluginLoader(plugins_dir=tmp_path, danger_analyzer=None)
        await loader.load_all()
        meta = loader.get("github")
        assert meta.name == "github"


class TestLoaderDangerAnalysis:
    async def test_invokes_danger_analyzer_per_plugin(self, tmp_path: Path):
        _write_plugin(tmp_path, "github", _SAMPLE_GITHUB)

        analyzer = AsyncMock()
        analyzer.analyze.return_value = (False, "ok")

        loader = PluginLoader(plugins_dir=tmp_path, danger_analyzer=analyzer)
        await loader.load_all()

        analyzer.analyze.assert_awaited_once()
        assert loader.danger_map["github"] is False

    async def test_records_dangerous_verdict(self, tmp_path: Path):
        _write_plugin(tmp_path, "github", _SAMPLE_GITHUB)

        analyzer = AsyncMock()
        analyzer.analyze.return_value = (True, "uses httpx")

        loader = PluginLoader(plugins_dir=tmp_path, danger_analyzer=analyzer)
        await loader.load_all()

        assert loader.danger_map["github"] is True


class TestScanNew:
    async def test_only_loads_new_plugins(self, tmp_path: Path):
        _write_plugin(tmp_path, "github", _SAMPLE_GITHUB)
        loader = PluginLoader(plugins_dir=tmp_path, danger_analyzer=None)
        await loader.load_all()

        _write_plugin(tmp_path, "notion", _SAMPLE_NOTION)
        new = await loader.scan_new()

        assert set(new.keys()) == {"notion"}
        assert set(loader._registry.keys()) == {"github", "notion"}  # noqa: SLF001
