"""Integration tests for the sentry plugin capabilities, dispatched through
PluginRunner exactly as the framework will at runtime. httpx is mocked via
respx; no real Sentry calls."""

from __future__ import annotations

import hashlib
import hmac
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
from plugin.sentry import plugin as sentry_module

API = "https://sentry.io/api/0"
WORKSPACE = uuid.uuid4()
SECRET = "shhh-client-secret"
P = sentry_module.p  # the PluginBuilder


class _Ctx:
    """Duck-typed SkillContext — plugin code only reads credentials + config."""

    def __init__(self, **kw: Any) -> None:
        self.credentials: dict[str, Any] = kw.get("credentials", {"auth_token": "sntrys_tok"})
        self.config: dict[str, Any] = kw.get("config", {})


def _runner() -> PluginRunner:
    return PluginRunner()


def _sign(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


# ── plugin metadata ───────────────────────────────────────────────────────


class TestPluginMeta:
    def test_name_and_jurisdiction(self):
        assert P.meta.name == "sentry"
        assert P.meta.data_jurisdiction == "us"

    def test_declares_credentials(self):
        names = {c["name"] for c in P.meta.credentials}
        assert "auth_token" in names
        assert "client_secret" in names

    def test_outbound_declares_t2_compensation(self):
        cap = next(c for c in P.meta.outbounds if "sentry_issue_update" in c.artifact_types)
        assert cap.compensation_tier == "t2_trail"
        assert cap.compensation_supported is True

    def test_mcp_exposed_action(self):
        assert P.meta.actions["resolve_issue"].mcp_exposed is True
        # M2 — new read-only @p.action exposed mid-run.
        assert P.meta.actions["list_issues"].mcp_exposed is True

    def test_has_setup(self):
        assert P.meta.setup_fn is not None


# ── inbound webhook ─────────────────────────────────────────────────────────


class TestInbound:
    async def test_inbound_parses_issue_webhook(self):
        body = json.dumps(
            {
                "id": "WH-1",
                "action": "created",
                "data": {"issue": {"id": "100001", "title": "TypeError", "level": "error"}},
            }
        ).encode()
        payload = {
            "workspace_id": WORKSPACE,
            "headers": {
                "Sentry-Hook-Signature": _sign(SECRET, body),
                "Sentry-Hook-Resource": "issue",
            },
            "raw_body": body,
        }
        ctx = _Ctx(credentials={"auth_token": "t", "client_secret": SECRET})
        evt = await _runner().dispatch_inbound(
            P.meta, trigger_type="webhook", context=ctx, payload=payload
        )
        assert isinstance(evt, TriggerEvent)
        assert evt.idempotency_key == "sentry:issue:WH-1"
        assert evt.payload["issue_id"] == "100001"

    async def test_inbound_accepts_str_raw_body(self):
        body = json.dumps(
            {"id": "WH-str", "data": {"issue": {"id": "1", "title": "x"}}}
        )  # str, not bytes
        payload = {
            "workspace_id": WORKSPACE,
            "headers": {"Sentry-Hook-Resource": "issue"},
            "raw_body": body,
        }
        evt = await _runner().dispatch_inbound(
            P.meta,
            trigger_type="webhook",
            context=_Ctx(credentials={"auth_token": "t"}),  # no secret → skip verify
            payload=payload,
        )
        assert isinstance(evt, TriggerEvent)
        assert evt.idempotency_key == "sentry:issue:WH-str"

    async def test_inbound_unsupported_resource_returns_none(self):
        body = b'{"id":"WH-x","data":{}}'
        payload = {
            "workspace_id": WORKSPACE,
            "headers": {"Sentry-Hook-Resource": "installation"},
            "raw_body": body,
        }
        evt = await _runner().dispatch_inbound(
            P.meta,
            trigger_type="webhook",
            context=_Ctx(credentials={"auth_token": "t"}),
            payload=payload,
        )
        assert evt is None


# ── outbound resolve ────────────────────────────────────────────────────────


class TestOutbound:
    @respx.mock
    async def test_deliver_resolve_returns_handle(self):
        respx.put(f"{API}/issues/100001/").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "100001",
                    "status": "resolved",
                    "permalink": "https://sentry.io/org/proj/issues/100001/",
                },
            )
        )
        result = await _runner().dispatch_outbound(
            P.meta,
            artifact_type="sentry_issue_update",
            context=_Ctx(),
            event={"issue_id": "100001"},
        )
        assert result["artifact_type"] == "sentry_issue_update"
        assert result["external_ref"] == "sentry://issue/100001"
        assert result["url"] == "https://sentry.io/org/proj/issues/100001/"
        assert result["compensation_handle"] == {
            "kind": "issue_status",
            "issue_id": "100001",
        }

    @respx.mock
    async def test_deliver_resolve_error_path(self):
        respx.put(f"{API}/issues/100001/").mock(return_value=httpx.Response(403, text="forbidden"))
        with pytest.raises(PluginRunError, match="403"):
            await _runner().dispatch_outbound(
                P.meta,
                artifact_type="sentry_issue_update",
                context=_Ctx(),
                event={"issue_id": "100001"},
            )

    async def test_missing_token_raises(self):
        with pytest.raises(PluginRunError, match="auth_token"):
            await _runner().dispatch_outbound(
                P.meta,
                artifact_type="sentry_issue_update",
                context=_Ctx(credentials={}),
                event={"issue_id": "100001"},
            )


# ── compensation (idempotent) ──────────────────────────────────────────────


class TestCompensate:
    @respx.mock
    async def test_revert_resolve_reopens_issue(self):
        route = respx.put(f"{API}/issues/100001/").mock(
            return_value=httpx.Response(200, json={"id": "100001", "status": "unresolved"})
        )
        result = await _runner().dispatch_compensate(
            P.meta,
            artifact_type="sentry_issue_update",
            context=_Ctx(),
            handle={"kind": "issue_status", "issue_id": "100001"},
        )
        assert route.called
        sent = json.loads(route.calls.last.request.content)
        assert sent["status"] == "unresolved"
        assert result["already"] is False
        assert result["tier"] == "t2_trail"
        assert result["status"] in {"compensated", "partially_compensated"}

    @respx.mock
    async def test_revert_resolve_idempotent_when_already_gone(self):
        respx.put(f"{API}/issues/100001/").mock(
            side_effect=[
                httpx.Response(200, json={"id": "100001", "status": "unresolved"}),
                httpx.Response(404, text="not found"),
            ]
        )
        handle = {"kind": "issue_status", "issue_id": "100001"}
        first = await _runner().dispatch_compensate(
            P.meta, artifact_type="sentry_issue_update", context=_Ctx(), handle=handle
        )
        second = await _runner().dispatch_compensate(
            P.meta, artifact_type="sentry_issue_update", context=_Ctx(), handle=handle
        )
        assert first["already"] is False
        assert second["already"] is True  # 404 → already gone, still success
        assert second["status"] in {"compensated", "partially_compensated"}

    @respx.mock
    async def test_revert_resolve_other_error_raises(self):
        respx.put(f"{API}/issues/100001/").mock(return_value=httpx.Response(403, text="forbidden"))
        with pytest.raises(PluginRunError, match="403"):
            await _runner().dispatch_compensate(
                P.meta,
                artifact_type="sentry_issue_update",
                context=_Ctx(),
                handle={"kind": "issue_status", "issue_id": "100001"},
            )


# ── actions (mcp_exposed) ──────────────────────────────────────────────────


class TestActions:
    @respx.mock
    async def test_resolve_issue_action(self):
        respx.put(f"{API}/issues/100001/").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "100001",
                    "status": "resolved",
                    "permalink": "https://sentry.io/org/proj/issues/100001/",
                },
            )
        )
        result = await _runner().dispatch_action(
            P.meta,
            action_name="resolve_issue",
            context=_Ctx(),
            kwargs={"issue_id": "100001"},
        )
        assert result["issue_id"] == "100001"
        assert result["status"] == "resolved"
        assert result["external_ref"] == "sentry://issue/100001"

    async def test_resolve_issue_action_rejects_bad_schema(self):
        with pytest.raises(PluginRunError, match="schema"):
            await _runner().dispatch_action(
                P.meta,
                action_name="resolve_issue",
                context=_Ctx(),
                kwargs={},  # missing required issue_id
            )

    # M2 — new read-only list_issues action
    @respx.mock
    async def test_list_issues_action_returns_shaped_issues(self):
        respx.get(f"{API}/projects/myorg/myproj/issues/").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {
                        "id": "1001",
                        "title": "TypeError in handler",
                        "status": "unresolved",
                        "permalink": "https://sentry.io/myorg/myproj/issues/1001/",
                        "count": "42",
                        "culprit": "app.handler in process",
                    },
                ],
            )
        )
        result = await _runner().dispatch_action(
            P.meta,
            action_name="list_issues",
            context=_Ctx(),
            kwargs={"organization_slug": "myorg", "project_slug": "myproj"},
        )
        assert result["organization_slug"] == "myorg"
        assert result["project_slug"] == "myproj"
        assert result["query"] == "is:unresolved"
        assert result["count"] == 1
        assert result["issues"][0]["id"] == "1001"
        assert result["issues"][0]["title"] == "TypeError in handler"
        assert result["issues"][0]["status"] == "unresolved"
        assert result["issues"][0]["culprit"] == "app.handler in process"

    async def test_list_issues_action_requires_slugs(self):
        with pytest.raises(PluginRunError, match="schema"):
            await _runner().dispatch_action(
                P.meta,
                action_name="list_issues",
                context=_Ctx(),
                kwargs={"organization_slug": "myorg"},  # missing project_slug
            )

    @respx.mock
    async def test_list_issues_action_uses_custom_query(self):
        route = respx.get(f"{API}/projects/myorg/myproj/issues/").mock(
            return_value=httpx.Response(200, json=[])
        )
        await _runner().dispatch_action(
            P.meta,
            action_name="list_issues",
            context=_Ctx(),
            kwargs={
                "organization_slug": "myorg",
                "project_slug": "myproj",
                "query": "is:unresolved error.type:KeyError",
            },
        )
        assert route.called
        url = str(route.calls[0].request.url)
        assert "query=is" in url
        assert "KeyError" in url


# ── setup ──────────────────────────────────────────────────────────────────


class TestSetup:
    async def test_setup_stores_credentials_from_env(self, monkeypatch):
        monkeypatch.setenv("SENTRY_AUTH_TOKEN", "sntrys_token-value")
        monkeypatch.setenv("SENTRY_CLIENT_SECRET", "client-secret-value")
        store = AsyncMock()
        await P.meta.setup_fn(store)
        store.store.assert_awaited_once()
        args = store.store.await_args.args
        assert args[0] == "sentry"
        assert args[1]["auth_token"] == "sntrys_token-value"
        assert args[1]["client_secret"] == "client-secret-value"

    async def test_setup_token_only(self, monkeypatch):
        monkeypatch.setenv("SENTRY_AUTH_TOKEN", "sntrys_token-value")
        monkeypatch.delenv("SENTRY_CLIENT_SECRET", raising=False)
        store = AsyncMock()
        data = await P.meta.setup_fn(store)
        assert data["auth_token"] == "sntrys_token-value"
        assert "client_secret" not in data

    async def test_setup_requires_token(self, monkeypatch):
        monkeypatch.delenv("SENTRY_AUTH_TOKEN", raising=False)
        with pytest.raises(ValueError, match="SENTRY_AUTH_TOKEN"):
            await P.meta.setup_fn(AsyncMock())


# ── loader discovery ────────────────────────────────────────────────────────


class TestLoaderDiscovery:
    async def test_loader_discovers_sentry(self):
        impl_dir = Path(sentry_module.__file__).resolve().parents[1]
        loader = PluginLoader(impl_dir)
        registry = await loader.load_all()
        assert "sentry" in registry
        meta = registry["sentry"]
        assert any("sentry_issue_update" in c.artifact_types for c in meta.outbounds)
        assert meta.compensates  # has compensate handlers
