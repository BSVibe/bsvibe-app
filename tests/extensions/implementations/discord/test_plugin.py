"""Integration tests for the discord plugin capabilities, dispatched through
PluginRunner exactly as the framework will at runtime. httpx is mocked via
respx; Ed25519 inbound tests use an in-test throwaway keypair. No real Discord
calls."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import respx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from backend.extensions.implementations.discord import plugin as discord_module
from backend.extensions.plugin import PluginLoader, PluginRunError, PluginRunner
from backend.intake.schema import TriggerEvent

API = "https://discord.com/api/v10"
TOKEN = "bot-token-abc"
CHANNEL = "555"
WORKSPACE = uuid.uuid4()
TIMESTAMP = "1700000000"
P = discord_module.p  # the PluginBuilder


class _Ctx:
    """Duck-typed SkillContext — plugin code only reads credentials + config."""

    def __init__(self, **kw: Any) -> None:
        self.credentials: dict[str, Any] = kw.get("credentials", {"bot_token": TOKEN})
        self.config: dict[str, Any] = kw.get("config", {})


def _runner() -> PluginRunner:
    return PluginRunner()


def _keypair() -> tuple[Ed25519PrivateKey, str]:
    private = Ed25519PrivateKey.generate()
    return private, private.public_key().public_bytes_raw().hex()


def _command_interaction(interaction_id: str = "100") -> bytes:
    return json.dumps(
        {
            "id": interaction_id,
            "type": 2,
            "channel_id": CHANNEL,
            "member": {"user": {"id": "5", "bot": False}},
            "data": {"name": "ping"},
        }
    ).encode()


# ── plugin metadata ───────────────────────────────────────────────────────


class TestPluginMeta:
    def test_name_and_jurisdiction(self):
        assert P.meta.name == "discord"
        # Discord is US-operated → "us" (matches github/slack).
        assert P.meta.data_jurisdiction == "us"

    def test_declares_credentials(self):
        names = {c["name"] for c in P.meta.credentials}
        assert "bot_token" in names
        assert "public_key" in names

    def test_outbound_message_declares_t2_compensation(self):
        cap = next(c for c in P.meta.outbounds if "discord_message" in c.artifact_types)
        assert cap.compensation_tier == "t2_trail"
        assert cap.compensation_supported is True

    def test_mcp_exposed_action(self):
        assert P.meta.actions["send_message"].mcp_exposed is True

    def test_has_setup(self):
        assert P.meta.setup_fn is not None


# ── inbound interaction ──────────────────────────────────────────────────────


class TestInbound:
    async def test_inbound_parses_command(self):
        private, public_hex = _keypair()
        body = _command_interaction(interaction_id="100")
        sig = private.sign(TIMESTAMP.encode() + body).hex()
        payload = {
            "workspace_id": WORKSPACE,
            "headers": {
                "X-Signature-Ed25519": sig,
                "X-Signature-Timestamp": TIMESTAMP,
            },
            "raw_body": body,
        }
        ctx = _Ctx(credentials={"bot_token": TOKEN, "public_key": public_hex})
        evt = await _runner().dispatch_inbound(
            P.meta, trigger_type="webhook", context=ctx, payload=payload
        )
        assert isinstance(evt, TriggerEvent)
        assert evt.idempotency_key == "discord:100"

    async def test_inbound_accepts_str_raw_body(self):
        body = _command_interaction(interaction_id="101").decode()  # str, not bytes
        payload = {"workspace_id": WORKSPACE, "headers": {}, "raw_body": body}
        evt = await _runner().dispatch_inbound(
            P.meta,
            trigger_type="webhook",
            context=_Ctx(credentials={"bot_token": TOKEN}),  # no public_key → skip verify
            payload=payload,
        )
        assert isinstance(evt, TriggerEvent)
        assert evt.idempotency_key == "discord:101"

    async def test_inbound_bad_signature_rejected(self):
        _, public_hex = _keypair()
        other_private, _ = _keypair()
        body = _command_interaction()
        bad_sig = other_private.sign(TIMESTAMP.encode() + body).hex()
        payload = {
            "workspace_id": WORKSPACE,
            "headers": {
                "X-Signature-Ed25519": bad_sig,
                "X-Signature-Timestamp": TIMESTAMP,
            },
            "raw_body": body,
        }
        ctx = _Ctx(credentials={"bot_token": TOKEN, "public_key": public_hex})
        with pytest.raises(PluginRunError, match="mismatch"):
            await _runner().dispatch_inbound(
                P.meta, trigger_type="webhook", context=ctx, payload=payload
            )

    async def test_inbound_ping_returns_none(self):
        private, public_hex = _keypair()
        body = json.dumps({"id": "1", "type": 1}).encode()
        sig = private.sign(TIMESTAMP.encode() + body).hex()
        payload = {
            "workspace_id": WORKSPACE,
            "headers": {
                "X-Signature-Ed25519": sig,
                "X-Signature-Timestamp": TIMESTAMP,
            },
            "raw_body": body,
        }
        ctx = _Ctx(credentials={"bot_token": TOKEN, "public_key": public_hex})
        evt = await _runner().dispatch_inbound(
            P.meta, trigger_type="webhook", context=ctx, payload=payload
        )
        assert evt is None


# ── outbound message ───────────────────────────────────────────────────────


class TestOutbound:
    @respx.mock
    async def test_deliver_message_posts_and_returns_handle(self):
        respx.post(f"{API}/channels/{CHANNEL}/messages").mock(
            return_value=httpx.Response(200, json={"id": "42", "channel_id": CHANNEL})
        )
        result = await _runner().dispatch_outbound(
            P.meta,
            artifact_type="discord_message",
            context=_Ctx(),
            event={"channel_id": CHANNEL, "content": "hello"},
        )
        assert result["artifact_type"] == "discord_message"
        assert result["external_ref"] == "discord://555/42"
        assert result["compensation_handle"] == {
            "kind": "message",
            "channel_id": CHANNEL,
            "message_id": "42",
        }

    @respx.mock
    async def test_deliver_message_api_error_raises(self):
        respx.post(f"{API}/channels/{CHANNEL}/messages").mock(
            return_value=httpx.Response(403, json={"message": "Missing Permissions"})
        )
        with pytest.raises(PluginRunError, match="Missing Permissions"):
            await _runner().dispatch_outbound(
                P.meta,
                artifact_type="discord_message",
                context=_Ctx(),
                event={"channel_id": CHANNEL, "content": "hi"},
            )

    async def test_missing_token_raises(self):
        with pytest.raises(PluginRunError, match="bot_token"):
            await _runner().dispatch_outbound(
                P.meta,
                artifact_type="discord_message",
                context=_Ctx(credentials={}),
                event={"channel_id": CHANNEL, "content": "hi"},
            )


# ── compensation (idempotent) ──────────────────────────────────────────────


class TestCompensate:
    @respx.mock
    async def test_delete_message_when_present(self):
        route = respx.delete(f"{API}/channels/{CHANNEL}/messages/42").mock(
            return_value=httpx.Response(204)
        )
        result = await _runner().dispatch_compensate(
            P.meta,
            artifact_type="discord_message",
            context=_Ctx(),
            handle={"kind": "message", "channel_id": CHANNEL, "message_id": "42"},
        )
        assert route.called
        assert result["already"] is False
        assert result["tier"] == "t2_trail"
        assert result["status"] in {"compensated", "partially_compensated"}

    @respx.mock
    async def test_delete_message_idempotent_when_already_gone(self):
        respx.delete(f"{API}/channels/{CHANNEL}/messages/42").mock(
            side_effect=[
                httpx.Response(204),
                httpx.Response(404, json={"message": "Unknown Message"}),
            ]
        )
        handle = {"kind": "message", "channel_id": CHANNEL, "message_id": "42"}
        first = await _runner().dispatch_compensate(
            P.meta, artifact_type="discord_message", context=_Ctx(), handle=handle
        )
        second = await _runner().dispatch_compensate(
            P.meta, artifact_type="discord_message", context=_Ctx(), handle=handle
        )
        assert first["already"] is False
        assert second["already"] is True  # 404 → already gone, still success
        assert second["status"] in {"compensated", "partially_compensated"}


# ── actions (mcp_exposed) ──────────────────────────────────────────────────


class TestActions:
    @respx.mock
    async def test_send_message_action(self):
        respx.post(f"{API}/channels/{CHANNEL}/messages").mock(
            return_value=httpx.Response(200, json={"id": "900", "channel_id": CHANNEL})
        )
        result = await _runner().dispatch_action(
            P.meta,
            action_name="send_message",
            context=_Ctx(),
            kwargs={"channel_id": CHANNEL, "content": "hi"},
        )
        assert result["message_id"] == "900"
        assert result["external_ref"] == "discord://555/900"

    async def test_send_message_action_rejects_bad_schema(self):
        with pytest.raises(PluginRunError, match="schema"):
            await _runner().dispatch_action(
                P.meta,
                action_name="send_message",
                context=_Ctx(),
                kwargs={"channel_id": CHANNEL},  # missing required content
            )


# ── setup ──────────────────────────────────────────────────────────────────


class TestSetup:
    async def test_setup_stores_credentials_from_env(self, monkeypatch):
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "bot-token-value")
        monkeypatch.setenv("DISCORD_PUBLIC_KEY", "deadbeef")
        store = AsyncMock()
        await P.meta.setup_fn(store)
        store.store.assert_awaited_once()
        args = store.store.await_args.args
        assert args[0] == "discord"
        assert args[1]["bot_token"] == "bot-token-value"
        assert args[1]["public_key"] == "deadbeef"

    async def test_setup_token_only(self, monkeypatch):
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "bot-token-value")
        monkeypatch.delenv("DISCORD_PUBLIC_KEY", raising=False)
        store = AsyncMock()
        data = await P.meta.setup_fn(store)
        assert data == {"bot_token": "bot-token-value"}
        assert "public_key" not in data

    async def test_setup_requires_token(self, monkeypatch):
        monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
        with pytest.raises(ValueError, match="DISCORD_BOT_TOKEN"):
            await P.meta.setup_fn(AsyncMock())


# ── loader discovery ────────────────────────────────────────────────────────


class TestLoaderDiscovery:
    async def test_loader_discovers_discord(self):
        impl_dir = Path(discord_module.__file__).resolve().parents[1]
        loader = PluginLoader(impl_dir)
        registry = await loader.load_all()
        assert "discord" in registry
        meta = registry["discord"]
        assert any("discord_message" in c.artifact_types for c in meta.outbounds)
        assert meta.compensates  # has compensate handlers
