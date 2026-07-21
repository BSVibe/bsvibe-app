"""Integration tests for the telegram plugin capabilities, dispatched through
PluginRunner exactly as the framework will at runtime. httpx is mocked via
respx; no real Telegram calls."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from backend.extensions.plugin import PluginLoader, PluginRunError, PluginRunner
from backend.workflow.domain.incoming import TriggerEvent
from plugin.telegram import plugin as telegram_module

API = "https://api.telegram.org"
TOKEN = "12345:abcdef"
BOT = f"{API}/bot{TOKEN}"
WORKSPACE = uuid.uuid4()
SECRET = "shhh-secret-token"
P = telegram_module.p  # the PluginBuilder


class _Ctx:
    """Duck-typed SkillContext — plugin code only reads credentials + config."""

    def __init__(self, **kw: Any) -> None:
        self.credentials: dict[str, Any] = kw.get("credentials", {"bot_token": TOKEN})
        self.config: dict[str, Any] = kw.get("config", {})


def _runner() -> PluginRunner:
    return PluginRunner()


def _message_update(update_id: int = 100) -> bytes:
    return json.dumps(
        {
            "update_id": update_id,
            "message": {
                "message_id": 11,
                "from": {"id": 5, "is_bot": False},
                "chat": {"id": 99, "type": "private"},
                "text": "hello bot",
            },
        }
    ).encode()


# ── plugin metadata ───────────────────────────────────────────────────────


class TestPluginMeta:
    def test_name_and_jurisdiction(self):
        assert P.meta.name == "telegram"
        # Telegram is multi-region → "unknown" (unspecified/global), not "us"/"eu".
        assert P.meta.data_jurisdiction == "unknown"

    def test_declares_credentials(self):
        names = {c["name"] for c in P.meta.credentials}
        assert "bot_token" in names
        assert "webhook_secret" in names

    def test_outbound_message_declares_t2_compensation(self):
        cap = next(c for c in P.meta.outbounds if "telegram_message" in c.artifact_types)
        assert cap.compensation_tier == "t2_trail"
        assert cap.compensation_supported is True

    def test_mcp_exposed_action(self):
        assert P.meta.actions["send_message"].mcp_exposed is True

    def test_has_setup(self):
        assert P.meta.setup_fn is not None


# ── inbound update ──────────────────────────────────────────────────────────


class TestInbound:
    async def test_inbound_parses_message(self):
        payload = {
            "workspace_id": WORKSPACE,
            "headers": {"X-Telegram-Bot-Api-Secret-Token": SECRET},
            "raw_body": _message_update(update_id=100),
        }
        ctx = _Ctx(credentials={"bot_token": TOKEN, "webhook_secret": SECRET})
        evt = await _runner().dispatch_inbound(
            P.meta, trigger_type="webhook", context=ctx, payload=payload
        )
        assert isinstance(evt, TriggerEvent)
        assert evt.idempotency_key == "telegram:100"

    async def test_inbound_accepts_str_raw_body(self):
        body = _message_update(update_id=101).decode()  # str, not bytes
        payload = {"workspace_id": WORKSPACE, "headers": {}, "raw_body": body}
        evt = await _runner().dispatch_inbound(
            P.meta,
            trigger_type="webhook",
            context=_Ctx(credentials={"bot_token": TOKEN}),  # no webhook_secret → skip verify
            payload=payload,
        )
        assert isinstance(evt, TriggerEvent)
        assert evt.idempotency_key == "telegram:101"

    async def test_inbound_bad_secret_rejected(self):
        payload = {
            "workspace_id": WORKSPACE,
            "headers": {"X-Telegram-Bot-Api-Secret-Token": "wrong"},
            "raw_body": _message_update(),
        }
        ctx = _Ctx(credentials={"bot_token": TOKEN, "webhook_secret": SECRET})
        with pytest.raises(PluginRunError, match="mismatch"):
            await _runner().dispatch_inbound(
                P.meta, trigger_type="webhook", context=ctx, payload=payload
            )

    async def test_inbound_non_message_returns_none(self):
        body = json.dumps({"update_id": 5, "callback_query": {"id": "c"}}).encode()
        payload = {"workspace_id": WORKSPACE, "headers": {}, "raw_body": body}
        evt = await _runner().dispatch_inbound(
            P.meta,
            trigger_type="webhook",
            context=_Ctx(credentials={"bot_token": TOKEN}),
            payload=payload,
        )
        assert evt is None


# ── outbound message ───────────────────────────────────────────────────────


class TestOutbound:
    @respx.mock
    async def test_deliver_message_sends_and_returns_handle(self):
        respx.post(f"{BOT}/sendMessage").mock(
            return_value=httpx.Response(
                200, json={"ok": True, "result": {"message_id": 42, "chat": {"id": 99}}}
            )
        )
        result = await _runner().dispatch_outbound(
            P.meta,
            artifact_type="telegram_message",
            context=_Ctx(),
            event={"chat_id": 99, "text": "hello"},
        )
        assert result["artifact_type"] == "telegram_message"
        assert result["external_ref"] == "telegram://99/42"
        assert result["compensation_handle"] == {
            "kind": "message",
            "chat_id": 99,
            "message_id": 42,
        }

    @respx.mock
    async def test_deliver_message_ok_false_raises(self):
        respx.post(f"{BOT}/sendMessage").mock(
            return_value=httpx.Response(
                200, json={"ok": False, "description": "Bad Request: chat not found"}
            )
        )
        with pytest.raises(PluginRunError, match="chat not found"):
            await _runner().dispatch_outbound(
                P.meta,
                artifact_type="telegram_message",
                context=_Ctx(),
                event={"chat_id": 0, "text": "hi"},
            )

    async def test_missing_token_raises(self):
        with pytest.raises(PluginRunError, match="bot_token"):
            await _runner().dispatch_outbound(
                P.meta,
                artifact_type="telegram_message",
                context=_Ctx(credentials={}),
                event={"chat_id": 99, "text": "hi"},
            )


# ── compensation (idempotent) ──────────────────────────────────────────────


class TestCompensate:
    @respx.mock
    async def test_delete_message_when_present(self):
        route = respx.post(f"{BOT}/deleteMessage").mock(
            return_value=httpx.Response(200, json={"ok": True, "result": True})
        )
        result = await _runner().dispatch_compensate(
            P.meta,
            artifact_type="telegram_message",
            context=_Ctx(),
            handle={"kind": "message", "chat_id": 99, "message_id": 42},
        )
        assert route.called
        assert result["already"] is False
        assert result["tier"] == "t2_trail"
        assert result["status"] in {"compensated", "partially_compensated"}

    @respx.mock
    async def test_delete_message_idempotent_when_already_gone(self):
        respx.post(f"{BOT}/deleteMessage").mock(
            side_effect=[
                httpx.Response(200, json={"ok": True, "result": True}),
                httpx.Response(
                    200,
                    json={
                        "ok": False,
                        "description": "Bad Request: message to delete not found",
                    },
                ),
            ]
        )
        handle = {"kind": "message", "chat_id": 99, "message_id": 42}
        first = await _runner().dispatch_compensate(
            P.meta, artifact_type="telegram_message", context=_Ctx(), handle=handle
        )
        second = await _runner().dispatch_compensate(
            P.meta, artifact_type="telegram_message", context=_Ctx(), handle=handle
        )
        assert first["already"] is False
        assert second["already"] is True  # not-found → already gone, still success
        assert second["status"] in {"compensated", "partially_compensated"}


# ── actions (mcp_exposed) ──────────────────────────────────────────────────


class TestActions:
    @respx.mock
    async def test_send_message_action(self):
        respx.post(f"{BOT}/sendMessage").mock(
            return_value=httpx.Response(
                200, json={"ok": True, "result": {"message_id": 900, "chat": {"id": 99}}}
            )
        )
        result = await _runner().dispatch_action(
            P.meta,
            action_name="send_message",
            context=_Ctx(),
            kwargs={"chat_id": 99, "text": "hi"},
        )
        assert result["message_id"] == 900
        assert result["external_ref"] == "telegram://99/900"

    async def test_send_message_action_rejects_bad_schema(self):
        with pytest.raises(PluginRunError, match="schema"):
            await _runner().dispatch_action(
                P.meta,
                action_name="send_message",
                context=_Ctx(),
                kwargs={"chat_id": 99},  # missing required text
            )


# ── callback capabilities (inbound approve/reject) ─────────────────────────


class TestCallbackCapabilities:
    def test_callback_actions_are_not_mcp_exposed(self):
        # Internal capabilities — never surfaced as MCP/agent tools.
        for name in ("parse_callback", "answer_callback_query", "edit_message_text"):
            assert name in P.meta.actions
            assert P.meta.actions[name].mcp_exposed is False

    async def test_parse_callback_action_returns_structured_fields(self):
        body = {
            "update_id": 1,
            "callback_query": {
                "id": "cbq",
                "from": {"id": 7},
                "message": {"message_id": 3, "chat": {"id": 9, "type": "private"}},
                "data": "apv:D1",
            },
        }
        parsed = await _runner().dispatch_action(
            P.meta,
            action_name="parse_callback",
            context=_Ctx(),
            kwargs={"body": body},
        )
        assert parsed["verb"] == "apv"
        assert parsed["deliverable_id"] == "D1"
        assert parsed["from_id"] == 7
        assert parsed["chat_type"] == "private"

    @respx.mock
    async def test_answer_callback_query_action(self):
        route = respx.post(f"{BOT}/answerCallbackQuery").mock(
            return_value=httpx.Response(200, json={"ok": True, "result": True})
        )
        await _runner().dispatch_action(
            P.meta,
            action_name="answer_callback_query",
            context=_Ctx(),
            kwargs={"callback_query_id": "cbq", "text": "권한이 없어요"},
        )
        assert route.called
        body = json.loads(route.calls.last.request.content)
        assert body["callback_query_id"] == "cbq"
        assert body["text"] == "권한이 없어요"

    @respx.mock
    async def test_edit_message_text_action(self):
        route = respx.post(f"{BOT}/editMessageText").mock(
            return_value=httpx.Response(200, json={"ok": True, "result": {}})
        )
        await _runner().dispatch_action(
            P.meta,
            action_name="edit_message_text",
            context=_Ctx(),
            kwargs={
                "chat_id": 9,
                "message_id": 3,
                "text": "✅ 승인됨",
                "reply_markup": {"inline_keyboard": []},
            },
        )
        body = json.loads(route.calls.last.request.content)
        assert body["text"] == "✅ 승인됨"
        assert body["reply_markup"] == {"inline_keyboard": []}


# ── setup ──────────────────────────────────────────────────────────────────


class TestSetup:
    async def test_setup_stores_credentials_from_env(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "bot-token-value")
        monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "secret-value")
        store = AsyncMock()
        await P.meta.setup_fn(store)
        store.store.assert_awaited_once()
        args = store.store.await_args.args
        assert args[0] == "telegram"
        assert args[1]["bot_token"] == "bot-token-value"
        assert args[1]["webhook_secret"] == "secret-value"

    async def test_setup_requires_token(self, monkeypatch):
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        with pytest.raises(ValueError, match="TELEGRAM_BOT_TOKEN"):
            await P.meta.setup_fn(AsyncMock())


# ── loader discovery ────────────────────────────────────────────────────────


class TestLoaderDiscovery:
    async def test_loader_discovers_telegram(self):
        impl_dir = Path(telegram_module.__file__).resolve().parents[1]
        loader = PluginLoader(impl_dir)
        registry = await loader.load_all()
        assert "telegram" in registry
        meta = registry["telegram"]
        assert any("telegram_message" in c.artifact_types for c in meta.outbounds)
        assert meta.compensates  # has compensate handlers
