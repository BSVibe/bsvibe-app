"""Integration tests for the github plugin capabilities, dispatched through
PluginRunner exactly as the framework will at runtime. httpx is mocked via
respx; no real GitHub calls."""

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

from backend.extensions.implementations.github import plugin as github_module
from backend.extensions.plugin import PluginLoader, PluginRunError, PluginRunner
from backend.intake.schema import TriggerEvent

API = "https://api.github.com"
WORKSPACE = uuid.uuid4()
SECRET = "shhh"
P = github_module.p  # the PluginBuilder


class _Ctx:
    """Duck-typed SkillContext — plugin code only reads credentials + config."""

    def __init__(self, **kw: Any) -> None:
        self.credentials: dict[str, Any] = kw.get("credentials", {"token": "tok-123"})
        self.config: dict[str, Any] = kw.get("config", {})


def _runner() -> PluginRunner:
    return PluginRunner()


# ── plugin metadata ───────────────────────────────────────────────────────


class TestPluginMeta:
    def test_name_and_jurisdiction(self):
        assert P.meta.name == "github"
        assert P.meta.data_jurisdiction == "us"

    def test_declares_token_credential(self):
        names = {c["name"] for c in P.meta.credentials}
        assert "token" in names
        assert "webhook_secret" in names

    def test_outbound_pr_declares_t2_compensation(self):
        pr_cap = next(c for c in P.meta.outbounds if "pr" in c.artifact_types)
        assert pr_cap.compensation_tier == "t2_trail"
        assert pr_cap.compensation_supported is True

    def test_outbound_comment_declares_t1_compensation(self):
        c_cap = next(c for c in P.meta.outbounds if "issue_comment" in c.artifact_types)
        assert c_cap.compensation_tier == "t1_clean"
        assert c_cap.compensation_supported is True

    def test_mcp_exposed_actions(self):
        assert P.meta.actions["open_pr"].mcp_exposed is True
        assert P.meta.actions["comment"].mcp_exposed is True
        # M2 — new read-only @p.action exposed mid-run.
        assert P.meta.actions["list_issues"].mcp_exposed is True

    def test_has_setup(self):
        assert P.meta.setup_fn is not None


# ── inbound webhook ─────────────────────────────────────────────────────────


class TestInbound:
    async def test_inbound_parses_pr_webhook(self):
        body = json.dumps(
            {
                "action": "opened",
                "repository": {"full_name": "o/r"},
                "pull_request": {"number": 3},
                "sender": {"type": "User"},
            }
        ).encode()
        sig = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
        payload = {
            "workspace_id": WORKSPACE,
            "headers": {
                "X-GitHub-Event": "pull_request",
                "X-GitHub-Delivery": "del-3",
                "X-Hub-Signature-256": sig,
            },
            "raw_body": body,
        }
        ctx = _Ctx(credentials={"token": "t", "webhook_secret": SECRET})
        evt = await _runner().dispatch_inbound(
            P.meta, trigger_type="webhook", context=ctx, payload=payload
        )
        assert isinstance(evt, TriggerEvent)
        assert evt.idempotency_key == "del-3"

    async def test_inbound_accepts_str_raw_body(self):
        body = json.dumps(
            {
                "action": "opened",
                "repository": {"full_name": "o/r"},
                "pull_request": {"number": 9},
                "sender": {"type": "User"},
            }
        )  # str, not bytes
        payload = {
            "workspace_id": WORKSPACE,
            "headers": {"X-GitHub-Event": "pull_request", "X-GitHub-Delivery": "d-str"},
            "raw_body": body,
        }
        evt = await _runner().dispatch_inbound(
            P.meta,
            trigger_type="webhook",
            context=_Ctx(credentials={"token": "t"}),
            payload=payload,
        )
        assert isinstance(evt, TriggerEvent)
        assert evt.idempotency_key == "d-str"

    async def test_inbound_skip_returns_none(self):
        body = b'{"zen":"x"}'
        payload = {
            "workspace_id": WORKSPACE,
            "headers": {"X-GitHub-Event": "ping", "X-GitHub-Delivery": "p1"},
            "raw_body": body,
        }
        evt = await _runner().dispatch_inbound(
            P.meta, trigger_type="webhook", context=_Ctx(), payload=payload
        )
        assert evt is None


# ── outbound PR / comment ──────────────────────────────────────────────────


class TestOutbound:
    @respx.mock
    async def test_deliver_pr_opens_and_returns_handle(self):
        respx.post(f"{API}/repos/o/r/pulls").mock(
            return_value=httpx.Response(
                201, json={"number": 15, "html_url": "https://github.com/o/r/pull/15"}
            )
        )
        result = await _runner().dispatch_outbound(
            P.meta,
            artifact_type="pr",
            context=_Ctx(),
            event={"repo": "o/r", "head": "feat", "base": "main", "title": "T", "body": "B"},
        )
        assert result["external_ref"] == "github://o/r/pull/15"
        assert result["url"] == "https://github.com/o/r/pull/15"
        assert result["compensation_handle"] == {
            "kind": "pr",
            "owner": "o",
            "repo": "r",
            "number": 15,
        }

    @respx.mock
    async def test_deliver_pr_updates_existing(self):
        route = respx.patch(f"{API}/repos/o/r/pulls/15").mock(
            return_value=httpx.Response(
                200, json={"number": 15, "html_url": "https://github.com/o/r/pull/15"}
            )
        )
        result = await _runner().dispatch_outbound(
            P.meta,
            artifact_type="pr",
            context=_Ctx(),
            event={"repo": "o/r", "pr_number": 15, "title": "New", "body": "B"},
        )
        assert route.called
        assert result["compensation_handle"]["number"] == 15

    @respx.mock
    async def test_deliver_comment_returns_handle(self):
        respx.post(f"{API}/repos/o/r/issues/7/comments").mock(
            return_value=httpx.Response(201, json={"id": 99, "html_url": "u"})
        )
        result = await _runner().dispatch_outbound(
            P.meta,
            artifact_type="issue_comment",
            context=_Ctx(),
            event={"repo": "o/r", "issue_number": 7, "body": "hi"},
        )
        assert result["compensation_handle"] == {
            "kind": "comment",
            "owner": "o",
            "repo": "r",
            "comment_id": 99,
        }

    async def test_missing_token_raises(self):
        with pytest.raises(PluginRunError):
            await _runner().dispatch_outbound(
                P.meta,
                artifact_type="pr",
                context=_Ctx(credentials={}),
                event={"repo": "o/r", "head": "f", "title": "T"},
            )

    async def test_invalid_repo_raises(self):
        with pytest.raises(PluginRunError, match="invalid repo"):
            await _runner().dispatch_outbound(
                P.meta,
                artifact_type="pr",
                context=_Ctx(),
                event={"repo": "no-slash", "head": "f", "title": "T"},
            )


# ── compensation (idempotent) ──────────────────────────────────────────────


class TestCompensate:
    @respx.mock
    async def test_close_pr_when_open(self):
        respx.get(f"{API}/repos/o/r/pulls/15").mock(
            return_value=httpx.Response(200, json={"number": 15, "state": "open"})
        )
        close = respx.patch(f"{API}/repos/o/r/pulls/15").mock(
            return_value=httpx.Response(200, json={"number": 15, "state": "closed"})
        )
        result = await _runner().dispatch_compensate(
            P.meta,
            artifact_type="pr",
            context=_Ctx(),
            handle={"kind": "pr", "owner": "o", "repo": "r", "number": 15},
        )
        assert close.called
        assert result["already"] is False
        assert result["tier"] == "t2_trail"

    @respx.mock
    async def test_close_pr_idempotent_when_already_closed(self):
        respx.get(f"{API}/repos/o/r/pulls/15").mock(
            return_value=httpx.Response(200, json={"number": 15, "state": "closed"})
        )
        patch = respx.patch(f"{API}/repos/o/r/pulls/15")
        result = await _runner().dispatch_compensate(
            P.meta,
            artifact_type="pr",
            context=_Ctx(),
            handle={"kind": "pr", "owner": "o", "repo": "r", "number": 15},
        )
        assert not patch.called  # no PATCH issued — already closed
        assert result["already"] is True
        assert result["status"] in {"compensated", "partially_compensated"}

    @respx.mock
    async def test_delete_comment_idempotent(self):
        route = respx.delete(f"{API}/repos/o/r/issues/comments/99").mock(
            side_effect=[httpx.Response(204), httpx.Response(404)]
        )
        handle = {"kind": "comment", "owner": "o", "repo": "r", "comment_id": 99}
        first = await _runner().dispatch_compensate(
            P.meta, artifact_type="issue_comment", context=_Ctx(), handle=handle
        )
        second = await _runner().dispatch_compensate(
            P.meta, artifact_type="issue_comment", context=_Ctx(), handle=handle
        )
        assert route.call_count == 2
        assert first["already"] is False
        assert second["already"] is True  # 404 → already gone, still success
        assert second["status"] == "compensated"


# ── actions (mcp_exposed) ──────────────────────────────────────────────────


class TestActions:
    @respx.mock
    async def test_open_pr_action(self):
        respx.post(f"{API}/repos/o/r/pulls").mock(
            return_value=httpx.Response(
                201, json={"number": 8, "html_url": "https://github.com/o/r/pull/8"}
            )
        )
        result = await _runner().dispatch_action(
            P.meta,
            action_name="open_pr",
            context=_Ctx(),
            kwargs={"repo": "o/r", "head": "feat", "title": "T", "body": "B"},
        )
        assert result["pr_number"] == 8

    @respx.mock
    async def test_comment_action(self):
        respx.post(f"{API}/repos/o/r/issues/5/comments").mock(
            return_value=httpx.Response(201, json={"id": 12, "html_url": "u"})
        )
        result = await _runner().dispatch_action(
            P.meta,
            action_name="comment",
            context=_Ctx(),
            kwargs={"repo": "o/r", "issue_number": 5, "body": "hi"},
        )
        assert result["comment_id"] == 12

    async def test_open_pr_action_rejects_bad_schema(self):
        with pytest.raises(PluginRunError, match="schema"):
            await _runner().dispatch_action(
                P.meta,
                action_name="open_pr",
                context=_Ctx(),
                kwargs={"repo": "o/r"},  # missing required head/title
            )

    # M2 — new read-only list_issues action
    @respx.mock
    async def test_list_issues_action_returns_shaped_issues(self):
        respx.get(f"{API}/repos/o/r/issues").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {
                        "number": 1,
                        "title": "Crash on boot",
                        "state": "open",
                        "html_url": f"{API}/repos/o/r/issues/1",
                        "user": {"login": "octo"},
                    },
                    # PR mixed into the issues list — must be filtered.
                    {
                        "number": 2,
                        "title": "PR",
                        "state": "open",
                        "html_url": f"{API}/repos/o/r/pull/2",
                        "pull_request": {"url": "x"},
                        "user": {"login": "octo"},
                    },
                ],
            )
        )
        result = await _runner().dispatch_action(
            P.meta,
            action_name="list_issues",
            context=_Ctx(),
            kwargs={"repo": "o/r"},
        )
        assert result["repo"] == "o/r"
        assert result["state"] == "open"
        assert result["count"] == 1, "PR (issues+pulls union) must be filtered"
        assert result["issues"] == [
            {
                "number": 1,
                "title": "Crash on boot",
                "state": "open",
                "url": f"{API}/repos/o/r/issues/1",
                "author": "octo",
            }
        ]

    async def test_list_issues_action_requires_repo(self):
        with pytest.raises(PluginRunError, match="schema"):
            await _runner().dispatch_action(
                P.meta,
                action_name="list_issues",
                context=_Ctx(),
                kwargs={"state": "open"},  # missing required repo
            )

    @respx.mock
    async def test_list_issues_caps_limit_at_50(self):
        # The cap is enforced inside the action body (max(1, min(limit, 50))).
        route = respx.get(f"{API}/repos/o/r/issues").mock(return_value=httpx.Response(200, json=[]))
        await _runner().dispatch_action(
            P.meta,
            action_name="list_issues",
            context=_Ctx(),
            kwargs={"repo": "o/r", "limit": 50},  # max allowed by schema
        )
        assert route.called
        # The request URL must carry per_page=50 (the capped limit).
        called_url = str(route.calls[0].request.url)
        assert "per_page=50" in called_url


# ── setup ──────────────────────────────────────────────────────────────────


class TestSetup:
    async def test_setup_stores_credentials_from_env(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_token_value")
        monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "wh-secret")
        store = AsyncMock()
        await P.meta.setup_fn(store)
        store.store.assert_awaited_once()
        args = store.store.await_args.args
        assert args[0] == "github"
        assert args[1]["token"] == "ghp_token_value"
        assert args[1]["webhook_secret"] == "wh-secret"

    async def test_setup_requires_token(self, monkeypatch):
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        with pytest.raises(ValueError, match="GITHUB_TOKEN"):
            await P.meta.setup_fn(AsyncMock())


# ── loader discovery ────────────────────────────────────────────────────────


class TestLoaderDiscovery:
    async def test_loader_discovers_github(self):
        impl_dir = Path(github_module.__file__).resolve().parents[1]
        loader = PluginLoader(impl_dir)
        registry = await loader.load_all()
        assert "github" in registry
        meta = registry["github"]
        assert any("pr" in c.artifact_types for c in meta.outbounds)
        assert meta.compensates  # has compensate handlers
