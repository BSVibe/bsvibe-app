"""Integration tests for the slack plugin capabilities, dispatched through
PluginRunner exactly as the framework will at runtime. httpx is mocked via
respx; no real Slack calls."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from backend.intake.schema import TriggerEvent
from backend.plugins import PluginLoader, PluginRunError, PluginRunner
from backend.plugins.implementations.slack import plugin as slack_module

API = "https://slack.com/api"
WORKSPACE = uuid.uuid4()
SECRET = "shhh-signing"
P = slack_module.p  # the PluginBuilder


class _Ctx:
    """Duck-typed SkillContext — plugin code only reads credentials + config."""

    def __init__(self, **kw: Any) -> None:
        self.credentials: dict[str, Any] = kw.get("credentials", {"bot_token": "xoxb-123"})
        self.config: dict[str, Any] = kw.get("config", {})


def _runner() -> PluginRunner:
    return PluginRunner()


def _sign(secret: str, timestamp: str, body: bytes) -> str:
    base = b"v0:" + timestamp.encode() + b":" + body
    return "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()


# ── plugin metadata ───────────────────────────────────────────────────────


class TestPluginMeta:
    def test_name_and_jurisdiction(self):
        assert P.meta.name == "slack"
        assert P.meta.data_jurisdiction == "us"

    def test_declares_credentials(self):
        names = {c["name"] for c in P.meta.credentials}
        assert "bot_token" in names
        assert "signing_secret" in names

    def test_outbound_message_declares_t2_compensation(self):
        cap = next(c for c in P.meta.outbounds if "slack_message" in c.artifact_types)
        assert cap.compensation_tier == "t2_trail"
        assert cap.compensation_supported is True

    def test_mcp_exposed_action(self):
        assert P.meta.actions["post_message"].mcp_exposed is True

    def test_has_setup(self):
        assert P.meta.setup_fn is not None


# ── inbound event ─────────────────────────────────────────────────────────


class TestInbound:
    async def test_inbound_parses_app_mention(self):
        body = json.dumps(
            {
                "type": "event_callback",
                "event_id": "Ev3",
                "event": {
                    "type": "app_mention",
                    "channel": "C9",
                    "user": "U1",
                    "text": "<@U0> hi",
                    "ts": "1700000000.000100",
                },
            }
        ).encode()
        ts = str(int(time.time()))
        payload = {
            "workspace_id": WORKSPACE,
            "headers": {
                "X-Slack-Request-Timestamp": ts,
                "X-Slack-Signature": _sign(SECRET, ts, body),
            },
            "raw_body": body,
        }
        ctx = _Ctx(credentials={"bot_token": "t", "signing_secret": SECRET})
        evt = await _runner().dispatch_inbound(
            P.meta, trigger_type="webhook", context=ctx, payload=payload
        )
        assert isinstance(evt, TriggerEvent)
        assert evt.idempotency_key == "Ev3"

    async def test_inbound_accepts_str_raw_body(self):
        body = json.dumps(
            {
                "type": "event_callback",
                "event_id": "Ev-str",
                "event": {"type": "app_mention", "channel": "C1", "ts": "1.1"},
            }
        )  # str, not bytes
        payload = {
            "workspace_id": WORKSPACE,
            "headers": {},
            "raw_body": body,
        }
        evt = await _runner().dispatch_inbound(
            P.meta,
            trigger_type="webhook",
            context=_Ctx(credentials={"bot_token": "t"}),
            payload=payload,
        )
        assert isinstance(evt, TriggerEvent)
        assert evt.idempotency_key == "Ev-str"

    async def test_inbound_url_verification_returns_none(self):
        body = b'{"type":"url_verification","challenge":"x"}'
        payload = {
            "workspace_id": WORKSPACE,
            "headers": {},
            "raw_body": body,
        }
        evt = await _runner().dispatch_inbound(
            P.meta, trigger_type="webhook", context=_Ctx(), payload=payload
        )
        assert evt is None


# ── outbound message ───────────────────────────────────────────────────────


class TestOutbound:
    @respx.mock
    async def test_deliver_message_posts_and_returns_handle(self):
        respx.post(f"{API}/chat.postMessage").mock(
            return_value=httpx.Response(
                200, json={"ok": True, "channel": "C1", "ts": "1700000000.000100"}
            )
        )
        result = await _runner().dispatch_outbound(
            P.meta,
            artifact_type="slack_message",
            context=_Ctx(),
            event={"channel": "C1", "text": "hello"},
        )
        assert result["artifact_type"] == "slack_message"
        assert result["external_ref"] == "slack://C1/1700000000.000100"
        assert result["compensation_handle"] == {
            "kind": "message",
            "channel": "C1",
            "ts": "1700000000.000100",
        }

    @respx.mock
    async def test_deliver_message_ok_false_raises(self):
        respx.post(f"{API}/chat.postMessage").mock(
            return_value=httpx.Response(200, json={"ok": False, "error": "channel_not_found"})
        )
        with pytest.raises(PluginRunError, match="channel_not_found"):
            await _runner().dispatch_outbound(
                P.meta,
                artifact_type="slack_message",
                context=_Ctx(),
                event={"channel": "bad", "text": "hi"},
            )

    async def test_missing_token_raises(self):
        with pytest.raises(PluginRunError, match="bot_token"):
            await _runner().dispatch_outbound(
                P.meta,
                artifact_type="slack_message",
                context=_Ctx(credentials={}),
                event={"channel": "C1", "text": "hi"},
            )


# ── compensation (idempotent) ──────────────────────────────────────────────


class TestCompensate:
    @respx.mock
    async def test_delete_message_when_present(self):
        route = respx.post(f"{API}/chat.delete").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )
        result = await _runner().dispatch_compensate(
            P.meta,
            artifact_type="slack_message",
            context=_Ctx(),
            handle={"kind": "message", "channel": "C1", "ts": "1.1"},
        )
        assert route.called
        assert result["already"] is False
        assert result["tier"] == "t2_trail"
        assert result["status"] in {"compensated", "partially_compensated"}

    @respx.mock
    async def test_delete_message_idempotent_when_already_gone(self):
        respx.post(f"{API}/chat.delete").mock(
            side_effect=[
                httpx.Response(200, json={"ok": True}),
                httpx.Response(200, json={"ok": False, "error": "message_not_found"}),
            ]
        )
        handle = {"kind": "message", "channel": "C1", "ts": "1.1"}
        first = await _runner().dispatch_compensate(
            P.meta, artifact_type="slack_message", context=_Ctx(), handle=handle
        )
        second = await _runner().dispatch_compensate(
            P.meta, artifact_type="slack_message", context=_Ctx(), handle=handle
        )
        assert first["already"] is False
        assert second["already"] is True  # message_not_found → already gone, still success
        assert second["status"] in {"compensated", "partially_compensated"}


# ── actions (mcp_exposed) ──────────────────────────────────────────────────


class TestActions:
    @respx.mock
    async def test_post_message_action(self):
        respx.post(f"{API}/chat.postMessage").mock(
            return_value=httpx.Response(
                200, json={"ok": True, "channel": "C1", "ts": "1700000000.000900"}
            )
        )
        result = await _runner().dispatch_action(
            P.meta,
            action_name="post_message",
            context=_Ctx(),
            kwargs={"channel": "C1", "text": "hi"},
        )
        assert result["ts"] == "1700000000.000900"
        assert result["external_ref"] == "slack://C1/1700000000.000900"

    async def test_post_message_action_rejects_bad_schema(self):
        with pytest.raises(PluginRunError, match="schema"):
            await _runner().dispatch_action(
                P.meta,
                action_name="post_message",
                context=_Ctx(),
                kwargs={"channel": "C1"},  # missing required text
            )


# ── setup ──────────────────────────────────────────────────────────────────


class TestSetup:
    async def test_setup_stores_credentials_from_env(self, monkeypatch):
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-token-value")
        monkeypatch.setenv("SLACK_SIGNING_SECRET", "sign-secret")
        store = AsyncMock()
        await P.meta.setup_fn(store)
        store.store.assert_awaited_once()
        args = store.store.await_args.args
        assert args[0] == "slack"
        assert args[1]["bot_token"] == "xoxb-token-value"
        assert args[1]["signing_secret"] == "sign-secret"

    async def test_setup_requires_token(self, monkeypatch):
        monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
        with pytest.raises(ValueError, match="SLACK_BOT_TOKEN"):
            await P.meta.setup_fn(AsyncMock())


# ── loader discovery ────────────────────────────────────────────────────────


class TestLoaderDiscovery:
    async def test_loader_discovers_slack(self):
        impl_dir = Path(slack_module.__file__).resolve().parents[1]
        loader = PluginLoader(impl_dir)
        registry = await loader.load_all()
        assert "slack" in registry
        meta = registry["slack"]
        assert any("slack_message" in c.artifact_types for c in meta.outbounds)
        assert meta.compensates  # has compensate handlers
