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

from backend.plugins import PluginLoader, PluginRunError, PluginRunner
from backend.plugins.implementations.notion import plugin as notion_module

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
