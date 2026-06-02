"""Integration tests for the GPT (ChatGPT) plugin — dispatched through
``PluginRunner`` exactly as the framework will at runtime. No external
APIs; the fixture JSON stands in for a real OpenAI export bundle.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from backend.extensions.plugin import PluginLoader, PluginRunError, PluginRunner
from plugin.gpt import plugin as gpt_module

P = gpt_module.p  # the PluginBuilder

FIXTURE = Path(__file__).parent / "fixtures" / "conversations_sample.json"


class _Knowledge:
    """Duck-typed ``KnowledgeBackend`` capturing every ``write_seed`` call."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def write_seed(self, source: str, data: dict[str, Any]) -> str:
        self.calls.append((source, data))
        return f"/seeds/{source}/{len(self.calls)}.md"


class _Ctx:
    """Duck-typed SkillContext — plugin reads config + knowledge."""

    def __init__(
        self,
        *,
        config: dict[str, Any] | None = None,
        knowledge: _Knowledge | None = None,
    ) -> None:
        self.credentials: dict[str, Any] = {}
        self.config: dict[str, Any] = config or {}
        self.knowledge = knowledge if knowledge is not None else _Knowledge()


def _runner() -> PluginRunner:
    return PluginRunner()


def _write_fixture_to(dest_dir: Path) -> Path:
    """Drop the canonical fixture into ``dest_dir/conversations.json``."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / "conversations.json"
    shutil.copy(FIXTURE, dest)
    return dest


# ── plugin metadata ───────────────────────────────────────────────────────


class TestPluginMeta:
    def test_name_and_jurisdiction(self):
        assert P.meta.name == "gpt"
        assert P.meta.data_jurisdiction == "local"

    def test_declares_no_required_credentials(self):
        required = [c for c in P.meta.credentials if c.get("required")]
        assert required == []

    def test_import_conversations_action_registered(self):
        assert "import_conversations" in P.meta.actions
        cap = P.meta.actions["import_conversations"]
        assert cap.input_schema is not None
        props = cap.input_schema["properties"]
        assert "export_path" in props
        assert "since" in props
        assert "region" in props
        assert "gpt_binding_id" in props

    def test_no_outbound_or_compensate(self):
        # Inbound-knowledge plugin only — must not declare outbound dispatch.
        assert P.meta.outbounds == []
        assert P.meta.compensates == {} or P.meta.compensates is None or not P.meta.compensates

    def test_has_setup(self):
        assert P.meta.setup_fn is not None


# ── import_conversations action ───────────────────────────────────────────


class TestImportConversations:
    @pytest.mark.asyncio
    async def test_imports_fixture_export(self, tmp_path):
        json_path = _write_fixture_to(tmp_path)
        knowledge = _Knowledge()
        ctx = _Ctx(knowledge=knowledge)
        result = await _runner().dispatch_action(
            P.meta,
            action_name="import_conversations",
            context=ctx,
            kwargs={
                "gpt_binding_id": "binding-x",
                "export_path": str(json_path),
            },
        )
        # Fixture: 2 valid + 1 skipped (missing id).
        assert result["conversations_count"] == 2
        assert result["skipped"] == 1
        # conv-abc-123 → 2 surviving (user+assistant, system skipped).
        # conv-def-456 → 3 (user+tool+assistant).
        assert result["messages_count"] == 5
        assert len(knowledge.calls) == 2
        sources = {c[0] for c in knowledge.calls}
        assert sources == {"gpt"}

    @pytest.mark.asyncio
    async def test_export_path_can_be_directory(self, tmp_path):
        _write_fixture_to(tmp_path)
        knowledge = _Knowledge()
        ctx = _Ctx(knowledge=knowledge)
        result = await _runner().dispatch_action(
            P.meta,
            action_name="import_conversations",
            context=ctx,
            kwargs={"export_path": str(tmp_path)},
        )
        assert result["conversations_count"] == 2

    @pytest.mark.asyncio
    async def test_source_ref_uses_binding_and_id(self, tmp_path):
        json_path = _write_fixture_to(tmp_path)
        knowledge = _Knowledge()
        ctx = _Ctx(knowledge=knowledge)
        await _runner().dispatch_action(
            P.meta,
            action_name="import_conversations",
            context=ctx,
            kwargs={
                "gpt_binding_id": "binding-x",
                "export_path": str(json_path),
            },
        )
        refs = {data["source_ref"] for _, data in knowledge.calls}
        assert refs == {
            "gpt://binding-x/conv-abc-123",
            "gpt://binding-x/conv-def-456",
        }

    @pytest.mark.asyncio
    async def test_since_filter_with_epoch(self, tmp_path):
        json_path = _write_fixture_to(tmp_path)
        knowledge = _Knowledge()
        ctx = _Ctx(knowledge=knowledge)
        # conv-abc update=1713187200, conv-def update=1713200000.
        result = await _runner().dispatch_action(
            P.meta,
            action_name="import_conversations",
            context=ctx,
            kwargs={
                "export_path": str(json_path),
                "since": 1713190000,
            },
        )
        assert result["conversations_count"] == 1
        _, data = knowledge.calls[0]
        assert "conv-def-456" in data["source_ref"]

    @pytest.mark.asyncio
    async def test_default_region_when_unset(self, tmp_path):
        json_path = _write_fixture_to(tmp_path)
        knowledge = _Knowledge()
        ctx = _Ctx(knowledge=knowledge)
        await _runner().dispatch_action(
            P.meta,
            action_name="import_conversations",
            context=ctx,
            kwargs={"export_path": str(json_path)},
        )
        _, data = knowledge.calls[0]
        assert data["region"] == "imported-gpt"

    @pytest.mark.asyncio
    async def test_default_region_from_binding_config(self, tmp_path):
        json_path = _write_fixture_to(tmp_path)
        knowledge = _Knowledge()
        ctx = _Ctx(knowledge=knowledge, config={"default_region": "research"})
        await _runner().dispatch_action(
            P.meta,
            action_name="import_conversations",
            context=ctx,
            kwargs={"export_path": str(json_path)},
        )
        _, data = knowledge.calls[0]
        assert data["region"] == "research"

    @pytest.mark.asyncio
    async def test_region_kwarg_overrides_binding_default(self, tmp_path):
        json_path = _write_fixture_to(tmp_path)
        knowledge = _Knowledge()
        ctx = _Ctx(knowledge=knowledge, config={"default_region": "research"})
        await _runner().dispatch_action(
            P.meta,
            action_name="import_conversations",
            context=ctx,
            kwargs={"export_path": str(json_path), "region": "personal"},
        )
        _, data = knowledge.calls[0]
        assert data["region"] == "personal"

    @pytest.mark.asyncio
    async def test_export_path_falls_back_to_binding_config(self, tmp_path):
        json_path = _write_fixture_to(tmp_path)
        knowledge = _Knowledge()
        ctx = _Ctx(knowledge=knowledge, config={"export_path": str(json_path)})
        result = await _runner().dispatch_action(
            P.meta,
            action_name="import_conversations",
            context=ctx,
            kwargs={},
        )
        assert result["conversations_count"] == 2

    @pytest.mark.asyncio
    async def test_export_path_missing_entirely_raises(self):
        ctx = _Ctx()
        with pytest.raises(PluginRunError, match="export_path"):
            await _runner().dispatch_action(
                P.meta,
                action_name="import_conversations",
                context=ctx,
                kwargs={},
            )

    @pytest.mark.asyncio
    async def test_missing_knowledge_raises(self, tmp_path):
        json_path = _write_fixture_to(tmp_path)
        ctx = _Ctx(knowledge=None)
        ctx.knowledge = None  # type: ignore[assignment]
        with pytest.raises(PluginRunError, match="knowledge"):
            await _runner().dispatch_action(
                P.meta,
                action_name="import_conversations",
                context=ctx,
                kwargs={"export_path": str(json_path)},
            )

    @pytest.mark.asyncio
    async def test_missing_file_raises(self, tmp_path):
        ctx = _Ctx()
        with pytest.raises(PluginRunError, match="not found"):
            await _runner().dispatch_action(
                P.meta,
                action_name="import_conversations",
                context=ctx,
                kwargs={"export_path": str(tmp_path / "nope.json")},
            )

    @pytest.mark.asyncio
    async def test_malformed_json_raises(self, tmp_path):
        bad = tmp_path / "conversations.json"
        bad.write_text("{not valid json", encoding="utf-8")
        ctx = _Ctx()
        with pytest.raises(PluginRunError, match="failed to parse"):
            await _runner().dispatch_action(
                P.meta,
                action_name="import_conversations",
                context=ctx,
                kwargs={"export_path": str(bad)},
            )

    @pytest.mark.asyncio
    async def test_seed_payload_carries_frontmatter(self, tmp_path):
        json_path = _write_fixture_to(tmp_path)
        knowledge = _Knowledge()
        ctx = _Ctx(knowledge=knowledge)
        await _runner().dispatch_action(
            P.meta,
            action_name="import_conversations",
            context=ctx,
            kwargs={"export_path": str(json_path)},
        )
        # First seed corresponds to conv-abc-123 (Brainstorming…).
        _, data = knowledge.calls[0]
        fm = data["frontmatter"]
        assert fm["conversation_id"] == "conv-abc-123"
        assert fm["source"] == "chatgpt.com"
        assert "Brainstorming about marketing" in data["title"]
        # Markdown body must contain the original message text.
        assert "Help me think about marketing" in data["content"]

    @pytest.mark.asyncio
    async def test_skips_conversation_when_write_seed_fails(self, tmp_path):
        json_path = _write_fixture_to(tmp_path)

        class _FlakeyKnowledge(_Knowledge):
            async def write_seed(self, source, data):  # type: ignore[override]
                self.calls.append((source, data))
                if "conv-def-456" in data["source_ref"]:
                    raise RuntimeError("boom")
                return f"/seeds/{source}/{len(self.calls)}.md"

        knowledge = _FlakeyKnowledge()
        ctx = _Ctx(knowledge=knowledge)
        result = await _runner().dispatch_action(
            P.meta,
            action_name="import_conversations",
            context=ctx,
            kwargs={"export_path": str(json_path)},
        )
        # conv-abc succeeded; conv-def write failed; missing-id skipped.
        assert result["conversations_count"] == 1
        assert result["skipped"] >= 2

    @pytest.mark.asyncio
    async def test_emits_audit_log_with_counts(self, tmp_path):
        import structlog

        json_path = _write_fixture_to(tmp_path)
        knowledge = _Knowledge()
        ctx = _Ctx(knowledge=knowledge)
        with structlog.testing.capture_logs() as logs:
            await _runner().dispatch_action(
                P.meta,
                action_name="import_conversations",
                context=ctx,
                kwargs={
                    "gpt_binding_id": "binding-x",
                    "export_path": str(json_path),
                },
            )
        events = [r for r in logs if r["event"] == "audit.knowledge.imported.gpt"]
        assert len(events) == 1
        rec = events[0]
        assert rec["conversations_count"] == 2
        assert rec["messages_count"] == 5
        assert rec["skipped"] == 1
        assert rec["region"] == "imported-gpt"
        assert rec["binding_id"] == "binding-x"

    @pytest.mark.asyncio
    async def test_empty_export_yields_zero_counts(self, tmp_path):
        empty = tmp_path / "conversations.json"
        empty.write_text(json.dumps([]), encoding="utf-8")
        knowledge = _Knowledge()
        ctx = _Ctx(knowledge=knowledge)
        result = await _runner().dispatch_action(
            P.meta,
            action_name="import_conversations",
            context=ctx,
            kwargs={"export_path": str(empty)},
        )
        assert result["conversations_count"] == 0
        assert result["messages_count"] == 0
        assert result["skipped"] == 0


# ── setup ──────────────────────────────────────────────────────────────────


class TestSetup:
    @pytest.mark.asyncio
    async def test_setup_persists_export_path_from_env(self, monkeypatch, tmp_path):
        monkeypatch.setenv("GPT_EXPORT_PATH", str(tmp_path))
        monkeypatch.setenv("GPT_DEFAULT_REGION", "imported-gpt")
        store = AsyncMock()
        await P.meta.setup_fn(store)
        store.store.assert_awaited_once()
        args = store.store.await_args.args
        assert args[0] == "gpt"
        assert args[1]["export_path"] == str(tmp_path)
        assert args[1]["default_region"] == "imported-gpt"

    @pytest.mark.asyncio
    async def test_setup_optional_since(self, monkeypatch, tmp_path):
        monkeypatch.setenv("GPT_EXPORT_PATH", str(tmp_path))
        monkeypatch.setenv("GPT_SINCE", "2026-04-01T00:00:00Z")
        store = AsyncMock()
        data = await P.meta.setup_fn(store)
        assert data["since"] == "2026-04-01T00:00:00Z"

    @pytest.mark.asyncio
    async def test_setup_requires_export_path_env(self, monkeypatch):
        monkeypatch.delenv("GPT_EXPORT_PATH", raising=False)
        with pytest.raises(ValueError, match="GPT_EXPORT_PATH"):
            await P.meta.setup_fn(AsyncMock())


# ── loader discovery ────────────────────────────────────────────────────────


class TestLoaderDiscovery:
    @pytest.mark.asyncio
    async def test_loader_discovers_gpt(self):
        impl_dir = Path(gpt_module.__file__).resolve().parents[1]
        loader = PluginLoader(impl_dir)
        registry = await loader.load_all()
        assert "gpt" in registry
        meta = registry["gpt"]
        assert "import_conversations" in meta.actions
