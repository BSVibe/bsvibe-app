"""Integration tests for the notion plugin capabilities, dispatched through
PluginRunner exactly as the framework will at runtime. httpx is mocked via
respx; no real Notion calls."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from backend.extensions.plugin import PluginLoader, PluginRunError, PluginRunner
from plugin.notion import plugin as notion_module

API = "https://api.notion.com"
P = notion_module.p  # the PluginBuilder


class _Ctx:
    """Duck-typed SkillContext — plugin code only reads credentials + config."""

    def __init__(self, **kw: Any) -> None:
        self.credentials: dict[str, Any] = kw.get("credentials", {"token": "secret-tok"})
        self.config: dict[str, Any] = kw.get("config", {})


def _runner() -> PluginRunner:
    return PluginRunner()


# ── plugin metadata ───────────────────────────────────────────────────────


class TestPluginMeta:
    def test_name_and_jurisdiction(self):
        assert P.meta.name == "notion"
        assert P.meta.data_jurisdiction == "us"

    def test_declares_token_credential(self):
        names = {c["name"] for c in P.meta.credentials}
        assert "token" in names

    def test_outbound_page_declares_t3_compensation(self):
        page_cap = next(c for c in P.meta.outbounds if "page" in c.artifact_types)
        assert page_cap.compensation_tier == "t3_new_artifact"
        assert page_cap.compensation_supported is True
        assert "page_image" in page_cap.artifact_types

    def test_has_compensate_for_page(self):
        assert any("page" in c.artifact_types for c in P.meta.compensates)

    def test_mcp_exposed_actions(self):
        assert P.meta.actions["create_page"].mcp_exposed is True
        assert P.meta.actions["append"].mcp_exposed is True

    def test_no_inbound(self):
        assert P.meta.inbounds == []

    def test_has_setup(self):
        assert P.meta.setup_fn is not None


# ── outbound page ────────────────────────────────────────────────────────────


class TestOutbound:
    @respx.mock
    async def test_deliver_page_creates_and_returns_handle(self):
        respx.post(f"{API}/v1/pages").mock(
            return_value=httpx.Response(
                200, json={"id": "page-9", "url": "https://notion.so/page-9"}
            )
        )
        result = await _runner().dispatch_outbound(
            P.meta,
            artifact_type="page",
            context=_Ctx(),
            event={"parent_page_id": "par-1", "title": "Spec", "body": "details"},
        )
        assert result["artifact_type"] == "page"
        assert result["external_ref"] == "notion://page/page-9"
        assert result["url"] == "https://notion.so/page-9"
        assert result["compensation_handle"] == {"kind": "page", "page_id": "page-9"}

    @respx.mock
    async def test_deliver_page_uses_parent_from_config(self):
        route = respx.post(f"{API}/v1/pages").mock(
            return_value=httpx.Response(200, json={"id": "page-2", "url": "u"})
        )
        result = await _runner().dispatch_outbound(
            P.meta,
            artifact_type="page_image",
            context=_Ctx(config={"notion_parent_page_id": "cfg-par"}),
            event={"title": "T"},
        )
        assert b"cfg-par" in route.calls.last.request.content
        assert result["compensation_handle"]["page_id"] == "page-2"

    async def test_missing_token_raises(self):
        with pytest.raises(PluginRunError):
            await _runner().dispatch_outbound(
                P.meta,
                artifact_type="page",
                context=_Ctx(credentials={}),
                event={"parent_page_id": "par-1", "title": "T"},
            )

    async def test_missing_parent_raises(self):
        with pytest.raises(PluginRunError, match="parent_page_id"):
            await _runner().dispatch_outbound(
                P.meta,
                artifact_type="page",
                context=_Ctx(),
                event={"title": "T"},
            )


# ── compensation (idempotent) ──────────────────────────────────────────────


class TestCompensate:
    @respx.mock
    async def test_archive_page(self):
        route = respx.patch(f"{API}/v1/pages/page-9").mock(
            return_value=httpx.Response(200, json={"id": "page-9", "archived": True})
        )
        result = await _runner().dispatch_compensate(
            P.meta,
            artifact_type="page",
            context=_Ctx(),
            handle={"kind": "page", "page_id": "page-9"},
        )
        assert route.called
        assert result["already"] is False
        assert result["tier"] == "t3_new_artifact"
        assert result["status"] == "partially_compensated"

    @respx.mock
    async def test_archive_page_idempotent_on_404(self):
        respx.patch(f"{API}/v1/pages/page-9").mock(
            side_effect=[httpx.Response(200, json={"archived": True}), httpx.Response(404)]
        )
        handle = {"kind": "page", "page_id": "page-9"}
        first = await _runner().dispatch_compensate(
            P.meta, artifact_type="page", context=_Ctx(), handle=handle
        )
        second = await _runner().dispatch_compensate(
            P.meta, artifact_type="page", context=_Ctx(), handle=handle
        )
        assert first["already"] is False
        assert second["already"] is True  # 404 → already gone, still success


# ── actions (mcp_exposed) ──────────────────────────────────────────────────


class TestActions:
    @respx.mock
    async def test_create_page_action(self):
        respx.post(f"{API}/v1/pages").mock(
            return_value=httpx.Response(200, json={"id": "page-8", "url": "https://notion.so/8"})
        )
        result = await _runner().dispatch_action(
            P.meta,
            action_name="create_page",
            context=_Ctx(),
            kwargs={"parent_page_id": "par-1", "title": "T", "body": "B"},
        )
        assert result["page_id"] == "page-8"
        assert result["external_ref"] == "notion://page/page-8"

    @respx.mock
    async def test_append_action(self):
        respx.patch(f"{API}/v1/blocks/page-8/children").mock(
            return_value=httpx.Response(200, json={"object": "list"})
        )
        result = await _runner().dispatch_action(
            P.meta,
            action_name="append",
            context=_Ctx(),
            kwargs={"page_id": "page-8", "text": "more"},
        )
        assert result["appended"] is True

    async def test_create_page_action_rejects_bad_schema(self):
        with pytest.raises(PluginRunError, match="schema"):
            await _runner().dispatch_action(
                P.meta,
                action_name="create_page",
                context=_Ctx(),
                kwargs={"parent_page_id": "par-1"},  # missing required title
            )


# ── import_pages action (knowledge ingest — Lift Q3-Notion) ────────────────


class _Knowledge:
    """Duck-typed ``KnowledgeBackend`` capturing every ``write_seed`` call."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def write_seed(self, source: str, data: dict[str, Any]) -> str:
        self.calls.append((source, data))
        return f"/seeds/{source}/{len(self.calls)}.md"


class _ImportCtx:
    """SkillContext substitute for import_pages tests."""

    def __init__(
        self,
        *,
        credentials: dict[str, Any] | None = None,
        config: dict[str, Any] | None = None,
        knowledge: _Knowledge | None = None,
    ) -> None:
        self.credentials = credentials if credentials is not None else {"token": "secret-tok"}
        self.config = config or {}
        self.knowledge = knowledge if knowledge is not None else _Knowledge()


def _page(page_id: str, title: str) -> dict[str, Any]:
    return {
        "object": "page",
        "id": page_id,
        "url": f"https://notion.so/{page_id}",
        "properties": {
            "title": {
                "type": "title",
                "title": [
                    {
                        "type": "text",
                        "plain_text": title,
                        "annotations": {},
                        "href": None,
                    }
                ],
            }
        },
    }


def _para_block(text: str, block_id: str = "blk") -> dict[str, Any]:
    return {
        "object": "block",
        "id": block_id,
        "type": "paragraph",
        "has_children": False,
        "paragraph": {
            "rich_text": [
                {
                    "type": "text",
                    "plain_text": text,
                    "annotations": {},
                    "href": None,
                }
            ]
        },
    }


def _empty_blocks_response() -> dict[str, Any]:
    return {"results": [], "has_more": False, "next_cursor": None}


class TestImportPagesAction:
    def test_action_registered(self):
        assert "import_pages" in P.meta.actions
        cap = P.meta.actions["import_pages"]
        assert cap.input_schema is not None
        # Schema must declare the optional binding_id/region/database_ids props.
        props = cap.input_schema["properties"]
        assert "region" in props

    @respx.mock
    async def test_imports_via_search(self):
        # /search returns one page; that page has one paragraph block.
        respx.post(f"{API}/v1/search").mock(
            return_value=httpx.Response(
                200,
                json={
                    "results": [_page("p-1", "Hello")],
                    "has_more": False,
                    "next_cursor": None,
                },
            )
        )
        respx.get(f"{API}/v1/blocks/p-1/children").mock(
            return_value=httpx.Response(
                200,
                json={
                    "results": [_para_block("body line")],
                    "has_more": False,
                    "next_cursor": None,
                },
            )
        )
        knowledge = _Knowledge()
        ctx = _ImportCtx(knowledge=knowledge)
        result = await _runner().dispatch_action(
            P.meta,
            action_name="import_pages",
            context=ctx,
            kwargs={"binding_id": "binding-x"},
        )
        assert result["pages_count"] == 1
        assert result["blocks_count"] == 1
        assert len(knowledge.calls) == 1
        source, data = knowledge.calls[0]
        assert source == "notion"
        assert data["title"] == "Hello"
        assert "body line" in data["content"]
        assert data["source_ref"] == "notion://binding-x/p-1"

    @respx.mock
    async def test_imports_via_database_ids(self):
        respx.post(f"{API}/v1/databases/db-1/query").mock(
            return_value=httpx.Response(
                200,
                json={
                    "results": [_page("p-a", "A"), _page("p-b", "B")],
                    "has_more": False,
                    "next_cursor": None,
                },
            )
        )
        respx.get(f"{API}/v1/blocks/p-a/children").mock(
            return_value=httpx.Response(200, json=_empty_blocks_response())
        )
        respx.get(f"{API}/v1/blocks/p-b/children").mock(
            return_value=httpx.Response(200, json=_empty_blocks_response())
        )
        knowledge = _Knowledge()
        ctx = _ImportCtx(
            knowledge=knowledge,
            config={"database_ids": ["db-1"]},
        )
        result = await _runner().dispatch_action(
            P.meta,
            action_name="import_pages",
            context=ctx,
            kwargs={"binding_id": "binding-x"},
        )
        assert result["pages_count"] == 2
        # Both seeded with the binding-scoped source_ref.
        refs = {data["source_ref"] for _, data in knowledge.calls}
        assert refs == {
            "notion://binding-x/p-a",
            "notion://binding-x/p-b",
        }

    @respx.mock
    async def test_default_region_from_binding_config(self):
        respx.post(f"{API}/v1/search").mock(
            return_value=httpx.Response(
                200,
                json={"results": [_page("p-1", "T")], "has_more": False, "next_cursor": None},
            )
        )
        respx.get(f"{API}/v1/blocks/p-1/children").mock(
            return_value=httpx.Response(200, json=_empty_blocks_response())
        )
        knowledge = _Knowledge()
        ctx = _ImportCtx(
            knowledge=knowledge,
            config={"default_region": "research"},
        )
        await _runner().dispatch_action(
            P.meta,
            action_name="import_pages",
            context=ctx,
            kwargs={"binding_id": "b1"},
        )
        _, data = knowledge.calls[0]
        assert data["region"] == "research"

    @respx.mock
    async def test_region_kwarg_overrides_binding_default(self):
        respx.post(f"{API}/v1/search").mock(
            return_value=httpx.Response(
                200,
                json={"results": [_page("p-1", "T")], "has_more": False, "next_cursor": None},
            )
        )
        respx.get(f"{API}/v1/blocks/p-1/children").mock(
            return_value=httpx.Response(200, json=_empty_blocks_response())
        )
        knowledge = _Knowledge()
        ctx = _ImportCtx(
            knowledge=knowledge,
            config={"default_region": "research"},
        )
        await _runner().dispatch_action(
            P.meta,
            action_name="import_pages",
            context=ctx,
            kwargs={"binding_id": "b1", "region": "personal"},
        )
        _, data = knowledge.calls[0]
        assert data["region"] == "personal"

    @respx.mock
    async def test_default_region_fallback_to_imported_notion(self):
        respx.post(f"{API}/v1/search").mock(
            return_value=httpx.Response(
                200,
                json={"results": [_page("p-1", "T")], "has_more": False, "next_cursor": None},
            )
        )
        respx.get(f"{API}/v1/blocks/p-1/children").mock(
            return_value=httpx.Response(200, json=_empty_blocks_response())
        )
        knowledge = _Knowledge()
        ctx = _ImportCtx(knowledge=knowledge)
        await _runner().dispatch_action(
            P.meta,
            action_name="import_pages",
            context=ctx,
            kwargs={"binding_id": "b1"},
        )
        _, data = knowledge.calls[0]
        assert data["region"] == "imported-notion"

    @respx.mock
    async def test_missing_token_raises(self):
        ctx = _ImportCtx(credentials={})
        with pytest.raises(PluginRunError):
            await _runner().dispatch_action(
                P.meta,
                action_name="import_pages",
                context=ctx,
                kwargs={"binding_id": "b1"},
            )

    @respx.mock
    async def test_missing_knowledge_raises(self):
        ctx = _ImportCtx()
        ctx.knowledge = None  # type: ignore[assignment]
        with pytest.raises(PluginRunError, match="knowledge"):
            await _runner().dispatch_action(
                P.meta,
                action_name="import_pages",
                context=ctx,
                kwargs={"binding_id": "b1"},
            )

    @respx.mock
    async def test_emits_audit_log(self):
        import structlog

        respx.post(f"{API}/v1/search").mock(
            return_value=httpx.Response(
                200,
                json={
                    "results": [_page("p-1", "T")],
                    "has_more": False,
                    "next_cursor": None,
                },
            )
        )
        respx.get(f"{API}/v1/blocks/p-1/children").mock(
            return_value=httpx.Response(
                200,
                json={
                    "results": [_para_block("x")],
                    "has_more": False,
                    "next_cursor": None,
                },
            )
        )
        knowledge = _Knowledge()
        ctx = _ImportCtx(knowledge=knowledge)
        with structlog.testing.capture_logs() as logs:
            await _runner().dispatch_action(
                P.meta,
                action_name="import_pages",
                context=ctx,
                kwargs={"binding_id": "binding-x"},
            )
        events = [r for r in logs if r["event"] == "audit.knowledge.imported.notion"]
        assert len(events) == 1
        rec = events[0]
        assert rec["pages_count"] == 1
        assert rec["blocks_count"] == 1
        assert rec["skipped"] == 0

    @respx.mock
    async def test_skips_page_on_block_fetch_error(self):
        # /search yields 2 pages; first one's block fetch 500s — we should
        # skip it and continue importing the second.
        respx.post(f"{API}/v1/search").mock(
            return_value=httpx.Response(
                200,
                json={
                    "results": [_page("bad", "Bad"), _page("good", "Good")],
                    "has_more": False,
                    "next_cursor": None,
                },
            )
        )
        respx.get(f"{API}/v1/blocks/bad/children").mock(
            return_value=httpx.Response(500, json={"message": "boom"})
        )
        respx.get(f"{API}/v1/blocks/good/children").mock(
            return_value=httpx.Response(
                200,
                json={
                    "results": [_para_block("ok")],
                    "has_more": False,
                    "next_cursor": None,
                },
            )
        )
        knowledge = _Knowledge()
        ctx = _ImportCtx(knowledge=knowledge)
        result = await _runner().dispatch_action(
            P.meta,
            action_name="import_pages",
            context=ctx,
            kwargs={"binding_id": "b"},
        )
        assert result["pages_count"] == 1
        assert result["skipped"] == 1
        # Only the good page got seeded.
        _, data = knowledge.calls[0]
        assert data["title"] == "Good"

    @respx.mock
    async def test_recurses_into_has_children_blocks(self):
        """A block with has_children=True triggers a second list_block_children call."""
        respx.post(f"{API}/v1/search").mock(
            return_value=httpx.Response(
                200,
                json={
                    "results": [_page("p-1", "T")],
                    "has_more": False,
                    "next_cursor": None,
                },
            )
        )
        parent = {
            "object": "block",
            "id": "parent",
            "type": "bulleted_list_item",
            "has_children": True,
            "bulleted_list_item": {
                "rich_text": [
                    {
                        "type": "text",
                        "plain_text": "top",
                        "annotations": {},
                        "href": None,
                    }
                ]
            },
        }
        respx.get(f"{API}/v1/blocks/p-1/children").mock(
            return_value=httpx.Response(
                200,
                json={"results": [parent], "has_more": False, "next_cursor": None},
            )
        )
        respx.get(f"{API}/v1/blocks/parent/children").mock(
            return_value=httpx.Response(
                200,
                json={
                    "results": [_para_block("nested", block_id="child-1")],
                    "has_more": False,
                    "next_cursor": None,
                },
            )
        )
        knowledge = _Knowledge()
        ctx = _ImportCtx(knowledge=knowledge)
        result = await _runner().dispatch_action(
            P.meta,
            action_name="import_pages",
            context=ctx,
            kwargs={"binding_id": "b"},
        )
        assert result["pages_count"] == 1
        # Nested content should be present in seeded markdown.
        _, data = knowledge.calls[0]
        assert "top" in data["content"]
        assert "nested" in data["content"]


# ── setup ──────────────────────────────────────────────────────────────────


class TestSetup:
    async def test_setup_stores_credentials_from_env(self, monkeypatch):
        monkeypatch.setenv("NOTION_TOKEN", "secret_value")
        store = AsyncMock()
        await P.meta.setup_fn(store)
        store.store.assert_awaited_once()
        args = store.store.await_args.args
        assert args[0] == "notion"
        assert args[1]["token"] == "secret_value"

    async def test_setup_requires_token(self, monkeypatch):
        monkeypatch.delenv("NOTION_TOKEN", raising=False)
        with pytest.raises(ValueError, match="NOTION_TOKEN"):
            await P.meta.setup_fn(AsyncMock())

    async def test_setup_optional_database_ids_and_region(self, monkeypatch):
        monkeypatch.setenv("NOTION_TOKEN", "secret_value")
        monkeypatch.setenv("NOTION_DATABASE_IDS", "db-1,db-2")
        monkeypatch.setenv("NOTION_DEFAULT_REGION", "research")
        store = AsyncMock()
        data = await P.meta.setup_fn(store)
        assert data["database_ids"] == ["db-1", "db-2"]
        assert data["default_region"] == "research"


# ── loader discovery ────────────────────────────────────────────────────────


class TestLoaderDiscovery:
    async def test_loader_discovers_notion(self):
        impl_dir = Path(notion_module.__file__).resolve().parents[1]
        loader = PluginLoader(impl_dir)
        registry = await loader.load_all()
        assert "notion" in registry
        meta = registry["notion"]
        assert any("page" in c.artifact_types for c in meta.outbounds)
        assert meta.compensates  # has compensate handlers
