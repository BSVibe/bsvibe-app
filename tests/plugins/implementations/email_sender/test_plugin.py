"""Integration tests for the email_sender plugin capabilities, dispatched
through PluginRunner exactly as the framework will at runtime. httpx is mocked
via respx; no real Resend calls."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from backend.plugins import PluginLoader, PluginRunError, PluginRunner
from backend.plugins.implementations.email_sender import plugin as email_module

API = "https://api.resend.com"
P = email_module.p  # the PluginBuilder


class _Ctx:
    """Duck-typed SkillContext — plugin code only reads credentials + config."""

    def __init__(self, **kw: Any) -> None:
        self.credentials: dict[str, Any] = kw.get(
            "credentials", {"api_key": "re_tok", "from": "BSVibe <no@bsvibe.dev>"}
        )
        self.config: dict[str, Any] = kw.get("config", {})


def _runner() -> PluginRunner:
    return PluginRunner()


# ── plugin metadata ───────────────────────────────────────────────────────


class TestPluginMeta:
    def test_name_and_jurisdiction(self):
        assert P.meta.name == "email-sender"
        assert P.meta.data_jurisdiction == "us"

    def test_declares_api_key_credential(self):
        names = {c["name"] for c in P.meta.credentials}
        assert "api_key" in names
        assert "from" in names

    def test_outbound_email_declares_t4_compensation(self):
        cap = next(c for c in P.meta.outbounds if "email" in c.artifact_types)
        assert cap.compensation_tier == "t4_irreversible"
        assert cap.compensation_supported is False

    def test_has_compensate_for_email(self):
        assert any("email" in c.artifact_types for c in P.meta.compensates)

    def test_send_email_action_is_mcp_exposed(self):
        assert P.meta.actions["send_email"].mcp_exposed is True

    def test_no_inbound(self):
        assert P.meta.inbounds == []

    def test_has_setup(self):
        assert P.meta.setup_fn is not None


# ── outbound email ────────────────────────────────────────────────────────────


class TestOutbound:
    @respx.mock
    async def test_deliver_email_sends_and_returns_handle(self):
        route = respx.post(f"{API}/emails").mock(
            return_value=httpx.Response(200, json={"id": "msg-9"})
        )
        result = await _runner().dispatch_outbound(
            P.meta,
            artifact_type="email",
            context=_Ctx(),
            event={"to": "dest@example.com", "subject": "Spec", "body": "<p>details</p>"},
        )
        assert route.called
        assert result["artifact_type"] == "email"
        assert result["external_ref"] == "resend://email/msg-9"
        assert result["compensation_handle"]["message_id"] == "msg-9"
        assert result["compensation_handle"]["to"] == "dest@example.com"

    @respx.mock
    async def test_deliver_email_as_text(self):
        route = respx.post(f"{API}/emails").mock(
            return_value=httpx.Response(200, json={"id": "msg-10"})
        )
        await _runner().dispatch_outbound(
            P.meta,
            artifact_type="email",
            context=_Ctx(),
            event={"to": "d@x.dev", "subject": "S", "body": "plain", "as_text": True},
        )
        content = route.calls.last.request.content
        assert b"plain" in content
        assert b'"html"' not in content

    @respx.mock
    async def test_deliver_email_uses_from_from_config(self):
        route = respx.post(f"{API}/emails").mock(
            return_value=httpx.Response(200, json={"id": "msg-11"})
        )
        await _runner().dispatch_outbound(
            P.meta,
            artifact_type="email",
            context=_Ctx(credentials={"api_key": "re_tok"}, config={"email_from": "cfg@x.dev"}),
            event={"to": "d@x.dev", "subject": "S", "body": "b"},
        )
        assert b"cfg@x.dev" in route.calls.last.request.content

    async def test_missing_api_key_raises(self):
        with pytest.raises(PluginRunError):
            await _runner().dispatch_outbound(
                P.meta,
                artifact_type="email",
                context=_Ctx(credentials={"from": "s@x.dev"}),
                event={"to": "d@x.dev", "subject": "S", "body": "b"},
            )

    async def test_missing_from_raises(self):
        with pytest.raises(PluginRunError, match="from"):
            await _runner().dispatch_outbound(
                P.meta,
                artifact_type="email",
                context=_Ctx(credentials={"api_key": "re_tok"}),
                event={"to": "d@x.dev", "subject": "S", "body": "b"},
            )

    @respx.mock
    async def test_outbound_http_error_surfaces_as_plugin_run_error(self):
        respx.post(f"{API}/emails").mock(
            return_value=httpx.Response(422, json={"message": "bad to"})
        )
        with pytest.raises(PluginRunError):
            await _runner().dispatch_outbound(
                P.meta,
                artifact_type="email",
                context=_Ctx(),
                event={"to": "bad", "subject": "S", "body": "b"},
            )


# ── compensation (T4 irreversible, idempotent no-op) ───────────────────────────


class TestCompensate:
    async def test_revert_email_records_uncompensable(self):
        result = await _runner().dispatch_compensate(
            P.meta,
            artifact_type="email",
            context=_Ctx(),
            handle={"kind": "email", "message_id": "msg-9"},
        )
        assert result["status"] == "uncompensable"
        assert result["tier"] == "t4_irreversible"
        assert result["already"] is True
        assert "msg-9" in result["summary"]

    async def test_revert_email_is_idempotent(self):
        handle = {"kind": "email", "message_id": "msg-9"}
        first = await _runner().dispatch_compensate(
            P.meta, artifact_type="email", context=_Ctx(), handle=handle
        )
        second = await _runner().dispatch_compensate(
            P.meta, artifact_type="email", context=_Ctx(), handle=handle
        )
        assert first == second

    @respx.mock
    async def test_revert_makes_no_remote_call(self):
        route = respx.post(f"{API}/emails")
        await _runner().dispatch_compensate(
            P.meta,
            artifact_type="email",
            context=_Ctx(),
            handle={"kind": "email", "message_id": "msg-9"},
        )
        assert not route.called


# ── actions (mcp_exposed) ──────────────────────────────────────────────────


class TestActions:
    @respx.mock
    async def test_send_email_action(self):
        route = respx.post(f"{API}/emails").mock(
            return_value=httpx.Response(200, json={"id": "msg-8"})
        )
        result = await _runner().dispatch_action(
            P.meta,
            action_name="send_email",
            context=_Ctx(),
            kwargs={"to": "d@x.dev", "subject": "T", "body": "<b>B</b>"},
        )
        assert route.called
        assert result["message_id"] == "msg-8"
        assert result["external_ref"] == "resend://email/msg-8"

    async def test_send_email_action_rejects_bad_schema(self):
        with pytest.raises(PluginRunError, match="schema"):
            await _runner().dispatch_action(
                P.meta,
                action_name="send_email",
                context=_Ctx(),
                kwargs={"to": "d@x.dev"},  # missing required subject + body
            )


# ── setup ──────────────────────────────────────────────────────────────────


class TestSetup:
    async def test_setup_stores_credentials_from_env(self, monkeypatch):
        monkeypatch.setenv("RESEND_API_KEY", "re_secret")
        monkeypatch.setenv("RESEND_FROM", "BSVibe <no@bsvibe.dev>")
        store = AsyncMock()
        await P.meta.setup_fn(store)
        store.store.assert_awaited_once()
        args = store.store.await_args.args
        assert args[0] == "email-sender"
        assert args[1]["api_key"] == "re_secret"
        assert args[1]["from"] == "BSVibe <no@bsvibe.dev>"

    async def test_setup_from_is_optional(self, monkeypatch):
        monkeypatch.setenv("RESEND_API_KEY", "re_secret")
        monkeypatch.delenv("RESEND_FROM", raising=False)
        store = AsyncMock()
        await P.meta.setup_fn(store)
        assert "from" not in store.store.await_args.args[1]

    async def test_setup_requires_api_key(self, monkeypatch):
        monkeypatch.delenv("RESEND_API_KEY", raising=False)
        with pytest.raises(ValueError, match="RESEND_API_KEY"):
            await P.meta.setup_fn(AsyncMock())


# ── loader discovery ────────────────────────────────────────────────────────


class TestLoaderDiscovery:
    async def test_loader_discovers_email_sender(self):
        impl_dir = Path(email_module.__file__).resolve().parents[1]
        loader = PluginLoader(impl_dir)
        registry = await loader.load_all()
        assert "email-sender" in registry
        meta = registry["email-sender"]
        assert any("email" in c.artifact_types for c in meta.outbounds)
        assert meta.compensates  # has compensate handlers
        assert meta.inbounds == []  # outbound-only
