"""Integration tests for the trello plugin capabilities, dispatched through
PluginRunner exactly as the framework will at runtime. httpx is mocked via
respx; no real Trello calls."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from backend.extensions.implementations.trello import plugin as trello_module
from backend.extensions.plugin import PluginLoader, PluginRunError, PluginRunner

API = "https://api.trello.com"
CARDS = f"{API}/1/cards"
P = trello_module.p  # the PluginBuilder


def _card_response(
    *,
    card_id: str = "card-1",
    url: str = "https://trello.com/c/abc/1-spec",
    short_url: str = "https://trello.com/c/abc",
) -> httpx.Response:
    return httpx.Response(
        200,
        json={"id": card_id, "name": "Spec", "url": url, "shortUrl": short_url},
    )


class _Ctx:
    """Duck-typed SkillContext — plugin code only reads credentials + config."""

    def __init__(self, **kw: Any) -> None:
        self.credentials: dict[str, Any] = kw.get(
            "credentials", {"api_key": "key-abc", "token": "tok-xyz"}
        )
        self.config: dict[str, Any] = kw.get("config", {})


def _runner() -> PluginRunner:
    return PluginRunner()


# ── plugin metadata ───────────────────────────────────────────────────────


class TestPluginMeta:
    def test_name_and_jurisdiction(self):
        assert P.meta.name == "trello"
        assert P.meta.data_jurisdiction == "us"

    def test_declares_api_key_and_token_credentials(self):
        names = {c["name"] for c in P.meta.credentials}
        assert {"api_key", "token"} <= names

    def test_outbound_card_declares_t3_compensation(self):
        cap = next(c for c in P.meta.outbounds if "card" in c.artifact_types)
        assert cap.compensation_tier == "t3_new_artifact"
        assert cap.compensation_supported is True

    def test_has_compensate_for_card(self):
        assert any("card" in c.artifact_types for c in P.meta.compensates)

    def test_mcp_exposed_action(self):
        assert P.meta.actions["create_card"].mcp_exposed is True

    def test_no_inbound(self):
        assert P.meta.inbounds == []

    def test_has_setup(self):
        assert P.meta.setup_fn is not None


# ── outbound card ─────────────────────────────────────────────────────────────


class TestOutbound:
    @respx.mock
    async def test_deliver_card_creates_and_returns_handle(self):
        respx.post(CARDS).mock(return_value=_card_response())
        result = await _runner().dispatch_outbound(
            P.meta,
            artifact_type="card",
            context=_Ctx(),
            event={"list_id": "list-1", "title": "Spec", "desc": "details"},
        )
        assert result["artifact_type"] == "card"
        assert result["external_ref"] == "trello://card/card-1"
        assert result["url"] == "https://trello.com/c/abc"  # shortUrl preferred
        assert result["compensation_handle"] == {"kind": "card", "card_id": "card-1"}

    @respx.mock
    async def test_deliver_card_uses_list_from_config_and_body_fallback(self):
        route = respx.post(CARDS).mock(return_value=_card_response(card_id="card-2"))
        result = await _runner().dispatch_outbound(
            P.meta,
            artifact_type="card",
            context=_Ctx(config={"trello_list_id": "cfg-list"}),
            event={"title": "T", "body": "from-body"},
        )
        sent = route.calls.last.request
        assert b"cfg-list" in sent.url.query
        assert b"from-body" in sent.url.query  # body used when desc missing
        assert result["compensation_handle"]["card_id"] == "card-2"

    @respx.mock
    async def test_deliver_card_falls_back_to_url_when_no_short_url(self):
        respx.post(CARDS).mock(
            return_value=httpx.Response(
                200, json={"id": "card-9", "url": "https://trello.com/c/zzz/9"}
            )
        )
        result = await _runner().dispatch_outbound(
            P.meta,
            artifact_type="card",
            context=_Ctx(),
            event={"list_id": "l", "title": "T"},
        )
        assert result["url"] == "https://trello.com/c/zzz/9"

    @respx.mock
    async def test_deliver_card_propagates_api_error(self):
        respx.post(CARDS).mock(return_value=httpx.Response(401, text="bad token"))
        with pytest.raises(PluginRunError):
            await _runner().dispatch_outbound(
                P.meta,
                artifact_type="card",
                context=_Ctx(),
                event={"list_id": "l", "title": "T"},
            )

    async def test_missing_credentials_raises(self):
        with pytest.raises(PluginRunError):
            await _runner().dispatch_outbound(
                P.meta,
                artifact_type="card",
                context=_Ctx(credentials={"api_key": "k"}),  # token missing
                event={"list_id": "l", "title": "T"},
            )

    async def test_missing_list_raises(self):
        with pytest.raises(PluginRunError, match="list_id"):
            await _runner().dispatch_outbound(
                P.meta,
                artifact_type="card",
                context=_Ctx(),
                event={"title": "T"},
            )


# ── compensation (idempotent) ──────────────────────────────────────────────


class TestCompensate:
    @respx.mock
    async def test_archive_card(self):
        route = respx.put(f"{CARDS}/card-1").mock(
            return_value=httpx.Response(200, json={"id": "card-1", "closed": True})
        )
        result = await _runner().dispatch_compensate(
            P.meta,
            artifact_type="card",
            context=_Ctx(),
            handle={"kind": "card", "card_id": "card-1"},
        )
        assert route.called
        assert result["already"] is False
        assert result["tier"] == "t3_new_artifact"
        assert result["status"] == "partially_compensated"

    @respx.mock
    async def test_archive_card_idempotent_on_not_found(self):
        respx.put(f"{CARDS}/card-1").mock(
            side_effect=[
                httpx.Response(200, json={"id": "card-1", "closed": True}),
                httpx.Response(404, text="not found"),
            ]
        )
        handle = {"kind": "card", "card_id": "card-1"}
        first = await _runner().dispatch_compensate(
            P.meta, artifact_type="card", context=_Ctx(), handle=handle
        )
        second = await _runner().dispatch_compensate(
            P.meta, artifact_type="card", context=_Ctx(), handle=handle
        )
        assert first["already"] is False
        assert second["already"] is True  # 404 → already gone, still success

    @respx.mock
    async def test_archive_card_reraises_on_other_error(self):
        respx.put(f"{CARDS}/card-1").mock(return_value=httpx.Response(429, text="rate limited"))
        with pytest.raises(PluginRunError):
            await _runner().dispatch_compensate(
                P.meta,
                artifact_type="card",
                context=_Ctx(),
                handle={"kind": "card", "card_id": "card-1"},
            )


# ── actions (mcp_exposed) ──────────────────────────────────────────────────


class TestActions:
    @respx.mock
    async def test_create_card_action(self):
        respx.post(CARDS).mock(return_value=_card_response(card_id="card-8"))
        result = await _runner().dispatch_action(
            P.meta,
            action_name="create_card",
            context=_Ctx(),
            kwargs={"list_id": "list-1", "title": "T", "desc": "B"},
        )
        assert result["card_id"] == "card-8"
        assert result["external_ref"] == "trello://card/card-8"
        assert result["url"] == "https://trello.com/c/abc"

    async def test_create_card_action_rejects_bad_schema(self):
        with pytest.raises(PluginRunError, match="schema"):
            await _runner().dispatch_action(
                P.meta,
                action_name="create_card",
                context=_Ctx(),
                kwargs={"list_id": "list-1"},  # missing required title
            )


# ── setup ──────────────────────────────────────────────────────────────────


class TestSetup:
    async def test_setup_stores_credentials_from_env(self, monkeypatch):
        monkeypatch.setenv("TRELLO_API_KEY", "key-value")
        monkeypatch.setenv("TRELLO_TOKEN", "token-value")
        monkeypatch.setenv("TRELLO_LIST_ID", "list-default")
        store = AsyncMock()
        await P.meta.setup_fn(store)
        store.store.assert_awaited_once()
        args = store.store.await_args.args
        assert args[0] == "trello"
        assert args[1]["api_key"] == "key-value"
        assert args[1]["token"] == "token-value"
        assert args[1]["trello_list_id"] == "list-default"

    async def test_setup_omits_list_id_when_unset(self, monkeypatch):
        monkeypatch.setenv("TRELLO_API_KEY", "key-value")
        monkeypatch.setenv("TRELLO_TOKEN", "token-value")
        monkeypatch.delenv("TRELLO_LIST_ID", raising=False)
        store = AsyncMock()
        await P.meta.setup_fn(store)
        assert "trello_list_id" not in store.store.await_args.args[1]

    async def test_setup_requires_api_key_and_token(self, monkeypatch):
        monkeypatch.setenv("TRELLO_API_KEY", "key-value")
        monkeypatch.delenv("TRELLO_TOKEN", raising=False)
        with pytest.raises(ValueError, match="TRELLO_TOKEN"):
            await P.meta.setup_fn(AsyncMock())


# ── loader discovery ────────────────────────────────────────────────────────


class TestLoaderDiscovery:
    async def test_loader_discovers_trello(self):
        impl_dir = Path(trello_module.__file__).resolve().parents[1]
        loader = PluginLoader(impl_dir)
        registry = await loader.load_all()
        assert "trello" in registry
        meta = registry["trello"]
        assert any("card" in c.artifact_types for c in meta.outbounds)
        assert meta.compensates  # has compensate handlers
