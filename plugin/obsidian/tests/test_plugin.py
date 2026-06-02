"""Integration tests for the Obsidian plugin — dispatched through
``PluginRunner`` exactly as the framework will at runtime. No real
filesystem mounts of the founder's Obsidian; every test stands up a tiny
vault under ``tmp_path``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from backend.extensions.plugin import PluginLoader, PluginRunError, PluginRunner
from plugin.obsidian import plugin as obsidian_module

P = obsidian_module.p  # the PluginBuilder


class _Knowledge:
    """Duck-typed ``KnowledgeBackend`` capturing every ``write_seed`` call."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def write_seed(self, source: str, data: dict[str, Any]) -> Path:
        self.calls.append((source, data))
        return Path(f"/seeds/{source}/{len(self.calls)}.md")


class _Ctx:
    """Duck-typed SkillContext — plugin reads config + knowledge."""

    def __init__(
        self,
        *,
        config: dict[str, Any] | None = None,
        knowledge: _Knowledge | None = None,
        logger: Any = None,
    ) -> None:
        self.credentials: dict[str, Any] = {}
        self.config: dict[str, Any] = config or {}
        self.knowledge = knowledge if knowledge is not None else _Knowledge()
        self.logger = logger


def _runner() -> PluginRunner:
    return PluginRunner()


def _write_md(root: Path, rel: str, body: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


# ── plugin metadata ───────────────────────────────────────────────────────


class TestPluginMeta:
    def test_name_and_jurisdiction(self):
        assert P.meta.name == "obsidian"
        # Vault sits on the founder's machine — local-only data.
        assert P.meta.data_jurisdiction == "local"

    def test_declares_no_required_credentials(self):
        # Obsidian is a local-filesystem connector; no API tokens.
        required = [c for c in P.meta.credentials if c.get("required")]
        assert required == []

    def test_import_vault_action_registered(self):
        assert "import_vault" in P.meta.actions
        cap = P.meta.actions["import_vault"]
        assert cap.input_schema is not None
        assert "vault_path" in cap.input_schema["properties"]

    def test_has_setup(self):
        assert P.meta.setup_fn is not None

    def test_no_outbound_or_compensate(self):
        # Inbound-knowledge plugin only — must not declare outbound dispatch.
        assert P.meta.outbounds == []
        assert P.meta.compensates == {} or P.meta.compensates is None or not P.meta.compensates


# ── import_vault action ───────────────────────────────────────────────────


class TestImportVault:
    @pytest.mark.asyncio
    async def test_imports_all_markdown_notes(self, tmp_path):
        _write_md(tmp_path, "a.md", "# a body")
        _write_md(tmp_path, "sub/b.md", "---\ntitle: B\n---\nb body\n")
        knowledge = _Knowledge()
        ctx = _Ctx(knowledge=knowledge)
        result = await _runner().dispatch_action(
            P.meta,
            action_name="import_vault",
            context=ctx,
            kwargs={"vault_path": str(tmp_path)},
        )
        assert result["notes_count"] == 2
        assert result["scanned_count"] == 2
        assert result["skipped_count"] == 0
        # write_seed called once per note, all under the "obsidian" source.
        assert len(knowledge.calls) == 2
        sources = {c[0] for c in knowledge.calls}
        assert sources == {"obsidian"}

    @pytest.mark.asyncio
    async def test_passes_frontmatter_to_seed_metadata(self, tmp_path):
        _write_md(
            tmp_path,
            "note.md",
            "---\ntitle: Hello\ntags:\n  - x\n---\nThe body.\n",
        )
        knowledge = _Knowledge()
        ctx = _Ctx(knowledge=knowledge)
        await _runner().dispatch_action(
            P.meta,
            action_name="import_vault",
            context=ctx,
            kwargs={"vault_path": str(tmp_path)},
        )
        _, data = knowledge.calls[0]
        assert data["title"] == "Hello"
        assert data["tags"] == ["x"]
        assert "The body." in data["content"]
        # source_ref preserves provenance (relative path in vault).
        assert data["source_ref"].endswith("note.md")
        assert data["source_ref"].startswith("obsidian://")

    @pytest.mark.asyncio
    async def test_excludes_default_dirs(self, tmp_path):
        _write_md(tmp_path, "real.md", "# real")
        _write_md(tmp_path, ".obsidian/cfg.md", "cfg")
        _write_md(tmp_path, "Templates/daily.md", "tpl")
        knowledge = _Knowledge()
        await _runner().dispatch_action(
            P.meta,
            action_name="import_vault",
            context=_Ctx(knowledge=knowledge),
            kwargs={"vault_path": str(tmp_path)},
        )
        assert len(knowledge.calls) == 1

    @pytest.mark.asyncio
    async def test_custom_exclude_patterns(self, tmp_path):
        _write_md(tmp_path, "keep.md", "# keep")
        _write_md(tmp_path, "skip/skip.md", "# skip")
        knowledge = _Knowledge()
        await _runner().dispatch_action(
            P.meta,
            action_name="import_vault",
            context=_Ctx(knowledge=knowledge),
            kwargs={
                "vault_path": str(tmp_path),
                "exclude_patterns": ["skip/**"],
            },
        )
        rels = [c[1]["source_ref"].split("/", maxsplit=2)[-1] for c in knowledge.calls]
        assert all("skip" not in r for r in rels)
        assert any("keep.md" in r for r in rels)

    @pytest.mark.asyncio
    async def test_region_routed_into_seed(self, tmp_path):
        _write_md(tmp_path, "a.md", "x")
        knowledge = _Knowledge()
        await _runner().dispatch_action(
            P.meta,
            action_name="import_vault",
            context=_Ctx(knowledge=knowledge, config={"default_region": "imported"}),
            kwargs={"vault_path": str(tmp_path), "region": "personal"},
        )
        # When both are set, kwarg region overrides default_region.
        _, data = knowledge.calls[0]
        assert data["region"] == "personal"

    @pytest.mark.asyncio
    async def test_region_falls_back_to_config_default(self, tmp_path):
        _write_md(tmp_path, "a.md", "x")
        knowledge = _Knowledge()
        await _runner().dispatch_action(
            P.meta,
            action_name="import_vault",
            context=_Ctx(knowledge=knowledge, config={"default_region": "imported"}),
            kwargs={"vault_path": str(tmp_path)},
        )
        _, data = knowledge.calls[0]
        assert data["region"] == "imported"

    @pytest.mark.asyncio
    async def test_missing_vault_path_in_args_falls_back_to_config(self, tmp_path):
        _write_md(tmp_path, "a.md", "x")
        knowledge = _Knowledge()
        ctx = _Ctx(knowledge=knowledge, config={"vault_path": str(tmp_path)})
        # Action allows omitting vault_path when the binding config supplies one.
        result = await _runner().dispatch_action(
            P.meta,
            action_name="import_vault",
            context=ctx,
            kwargs={},
        )
        assert result["notes_count"] == 1

    @pytest.mark.asyncio
    async def test_vault_path_missing_entirely_raises(self):
        ctx = _Ctx(knowledge=_Knowledge())
        with pytest.raises(PluginRunError, match="vault_path"):
            await _runner().dispatch_action(
                P.meta, action_name="import_vault", context=ctx, kwargs={}
            )

    @pytest.mark.asyncio
    async def test_no_knowledge_backend_raises(self, tmp_path):
        _write_md(tmp_path, "a.md", "x")
        ctx = _Ctx(knowledge=None)
        ctx.knowledge = None  # type: ignore[assignment]
        with pytest.raises(PluginRunError, match="knowledge"):
            await _runner().dispatch_action(
                P.meta,
                action_name="import_vault",
                context=ctx,
                kwargs={"vault_path": str(tmp_path)},
            )

    @pytest.mark.asyncio
    async def test_emits_audit_log_with_counts(self, tmp_path):
        import structlog

        _write_md(tmp_path, "a.md", "x")
        _write_md(tmp_path, "Templates/skip.md", "tpl")
        knowledge = _Knowledge()
        ctx = _Ctx(knowledge=knowledge)
        with structlog.testing.capture_logs() as logs:
            await _runner().dispatch_action(
                P.meta,
                action_name="import_vault",
                context=ctx,
                kwargs={"vault_path": str(tmp_path)},
            )
        events = [rec for rec in logs if rec["event"] == "audit.knowledge.imported.obsidian"]
        assert len(events) == 1
        rec = events[0]
        assert rec["notes_count"] == 1
        assert rec["scanned_count"] == 1
        assert rec["skipped_count"] == 0
        assert rec["region"] == "imported"


# ── setup ──────────────────────────────────────────────────────────────────


class TestSetup:
    @pytest.mark.asyncio
    async def test_setup_persists_vault_path_from_env(self, monkeypatch, tmp_path):
        monkeypatch.setenv("OBSIDIAN_VAULT_PATH", str(tmp_path))
        monkeypatch.setenv("OBSIDIAN_DEFAULT_REGION", "imported")
        store = AsyncMock()
        await P.meta.setup_fn(store)
        store.store.assert_awaited_once()
        args = store.store.await_args.args
        assert args[0] == "obsidian"
        assert args[1]["vault_path"] == str(tmp_path)
        assert args[1]["default_region"] == "imported"

    @pytest.mark.asyncio
    async def test_setup_requires_vault_path_env(self, monkeypatch):
        monkeypatch.delenv("OBSIDIAN_VAULT_PATH", raising=False)
        with pytest.raises(ValueError, match="OBSIDIAN_VAULT_PATH"):
            await P.meta.setup_fn(AsyncMock())


# ── loader discovery ────────────────────────────────────────────────────────


class TestLoaderDiscovery:
    @pytest.mark.asyncio
    async def test_loader_discovers_obsidian(self):
        impl_dir = Path(obsidian_module.__file__).resolve().parents[1]
        loader = PluginLoader(impl_dir)
        registry = await loader.load_all()
        assert "obsidian" in registry
        meta = registry["obsidian"]
        assert "import_vault" in meta.actions
