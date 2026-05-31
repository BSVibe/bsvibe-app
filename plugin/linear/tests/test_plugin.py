"""Integration tests for the linear plugin capabilities, dispatched through
PluginRunner exactly as the framework will at runtime. httpx is mocked via
respx; no real Linear calls."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from backend.extensions.plugin import PluginLoader, PluginRunError, PluginRunner
from plugin.linear import plugin as linear_module

API = "https://api.linear.app"
GQL = f"{API}/graphql"
P = linear_module.p  # the PluginBuilder


def _issue_create_response(
    *, issue_id: str = "iss-1", identifier: str = "ENG-12", url: str = "https://linear.app/i/ENG-12"
) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "data": {
                "issueCreate": {
                    "success": True,
                    "issue": {"id": issue_id, "identifier": identifier, "url": url},
                }
            }
        },
    )


def _not_found_response() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "errors": [{"message": "Entity not found", "extensions": {"code": "entityNotFound"}}]
        },
    )


class _Ctx:
    """Duck-typed SkillContext — plugin code only reads credentials + config."""

    def __init__(self, **kw: Any) -> None:
        self.credentials: dict[str, Any] = kw.get("credentials", {"api_key": "lin_api_secret"})
        self.config: dict[str, Any] = kw.get("config", {})


def _runner() -> PluginRunner:
    return PluginRunner()


# ── plugin metadata ───────────────────────────────────────────────────────


class TestPluginMeta:
    def test_name_and_jurisdiction(self):
        assert P.meta.name == "linear"
        assert P.meta.data_jurisdiction == "us"

    def test_declares_api_key_credential(self):
        names = {c["name"] for c in P.meta.credentials}
        assert "api_key" in names

    def test_outbound_issue_declares_t3_compensation(self):
        cap = next(c for c in P.meta.outbounds if "issue" in c.artifact_types)
        assert cap.compensation_tier == "t3_new_artifact"
        assert cap.compensation_supported is True

    def test_has_compensate_for_issue(self):
        assert any("issue" in c.artifact_types for c in P.meta.compensates)

    def test_mcp_exposed_action(self):
        assert P.meta.actions["create_issue"].mcp_exposed is True

    def test_no_inbound(self):
        assert P.meta.inbounds == []

    def test_has_setup(self):
        assert P.meta.setup_fn is not None


# ── outbound issue ────────────────────────────────────────────────────────────


class TestOutbound:
    @respx.mock
    async def test_deliver_issue_creates_and_returns_handle(self):
        respx.post(GQL).mock(return_value=_issue_create_response())
        result = await _runner().dispatch_outbound(
            P.meta,
            artifact_type="issue",
            context=_Ctx(),
            event={"team_id": "team-1", "title": "Spec", "description": "details"},
        )
        assert result["artifact_type"] == "issue"
        assert result["external_ref"] == "linear://issue/iss-1"
        assert result["url"] == "https://linear.app/i/ENG-12"
        assert result["compensation_handle"] == {
            "kind": "issue",
            "issue_id": "iss-1",
            "identifier": "ENG-12",
        }

    @respx.mock
    async def test_deliver_issue_uses_team_from_config_and_body_fallback(self):
        route = respx.post(GQL).mock(return_value=_issue_create_response(issue_id="iss-2"))
        result = await _runner().dispatch_outbound(
            P.meta,
            artifact_type="issue",
            context=_Ctx(config={"linear_team_id": "cfg-team"}),
            event={"title": "T", "body": "from-body"},
        )
        sent = route.calls.last.request
        assert b"cfg-team" in sent.content
        assert b"from-body" in sent.content  # body used when description missing
        assert result["compensation_handle"]["issue_id"] == "iss-2"

    @respx.mock
    async def test_deliver_issue_propagates_graphql_error(self):
        respx.post(GQL).mock(
            return_value=httpx.Response(200, json={"errors": [{"message": "boom"}]})
        )
        with pytest.raises(PluginRunError, match="boom"):
            await _runner().dispatch_outbound(
                P.meta,
                artifact_type="issue",
                context=_Ctx(),
                event={"team_id": "t1", "title": "T"},
            )

    async def test_missing_api_key_raises(self):
        with pytest.raises(PluginRunError):
            await _runner().dispatch_outbound(
                P.meta,
                artifact_type="issue",
                context=_Ctx(credentials={}),
                event={"team_id": "t1", "title": "T"},
            )

    async def test_missing_team_raises(self):
        with pytest.raises(PluginRunError, match="team_id"):
            await _runner().dispatch_outbound(
                P.meta,
                artifact_type="issue",
                context=_Ctx(),
                event={"title": "T"},
            )


# ── compensation (idempotent) ──────────────────────────────────────────────


class TestCompensate:
    @respx.mock
    async def test_archive_issue(self):
        route = respx.post(GQL).mock(
            return_value=httpx.Response(200, json={"data": {"issueArchive": {"success": True}}})
        )
        result = await _runner().dispatch_compensate(
            P.meta,
            artifact_type="issue",
            context=_Ctx(),
            handle={"kind": "issue", "issue_id": "iss-1"},
        )
        assert route.called
        assert result["already"] is False
        assert result["tier"] == "t3_new_artifact"
        assert result["status"] == "partially_compensated"

    @respx.mock
    async def test_archive_issue_idempotent_on_not_found(self):
        respx.post(GQL).mock(
            side_effect=[
                httpx.Response(200, json={"data": {"issueArchive": {"success": True}}}),
                _not_found_response(),
            ]
        )
        handle = {"kind": "issue", "issue_id": "iss-1"}
        first = await _runner().dispatch_compensate(
            P.meta, artifact_type="issue", context=_Ctx(), handle=handle
        )
        second = await _runner().dispatch_compensate(
            P.meta, artifact_type="issue", context=_Ctx(), handle=handle
        )
        assert first["already"] is False
        assert second["already"] is True  # entityNotFound → already gone, still success

    @respx.mock
    async def test_archive_issue_reraises_on_other_graphql_error(self):
        respx.post(GQL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "errors": [{"message": "rate limited", "extensions": {"code": "RATELIMITED"}}]
                },
            )
        )
        with pytest.raises(PluginRunError, match="rate limited"):
            await _runner().dispatch_compensate(
                P.meta,
                artifact_type="issue",
                context=_Ctx(),
                handle={"kind": "issue", "issue_id": "iss-1"},
            )


# ── actions (mcp_exposed) ──────────────────────────────────────────────────


class TestActions:
    @respx.mock
    async def test_create_issue_action(self):
        respx.post(GQL).mock(return_value=_issue_create_response(issue_id="iss-8"))
        result = await _runner().dispatch_action(
            P.meta,
            action_name="create_issue",
            context=_Ctx(),
            kwargs={"team_id": "team-1", "title": "T", "description": "B"},
        )
        assert result["issue_id"] == "iss-8"
        assert result["external_ref"] == "linear://issue/iss-8"
        assert result["identifier"] == "ENG-12"

    async def test_create_issue_action_rejects_bad_schema(self):
        with pytest.raises(PluginRunError, match="schema"):
            await _runner().dispatch_action(
                P.meta,
                action_name="create_issue",
                context=_Ctx(),
                kwargs={"team_id": "team-1"},  # missing required title
            )


# ── setup ──────────────────────────────────────────────────────────────────


class TestSetup:
    async def test_setup_stores_credentials_from_env(self, monkeypatch):
        monkeypatch.setenv("LINEAR_API_KEY", "lin_api_value")
        monkeypatch.setenv("LINEAR_TEAM_ID", "team-default")
        store = AsyncMock()
        await P.meta.setup_fn(store)
        store.store.assert_awaited_once()
        args = store.store.await_args.args
        assert args[0] == "linear"
        assert args[1]["api_key"] == "lin_api_value"
        assert args[1]["linear_team_id"] == "team-default"

    async def test_setup_omits_team_id_when_unset(self, monkeypatch):
        monkeypatch.setenv("LINEAR_API_KEY", "lin_api_value")
        monkeypatch.delenv("LINEAR_TEAM_ID", raising=False)
        store = AsyncMock()
        await P.meta.setup_fn(store)
        assert "linear_team_id" not in store.store.await_args.args[1]

    async def test_setup_requires_api_key(self, monkeypatch):
        monkeypatch.delenv("LINEAR_API_KEY", raising=False)
        with pytest.raises(ValueError, match="LINEAR_API_KEY"):
            await P.meta.setup_fn(AsyncMock())


# ── loader discovery ────────────────────────────────────────────────────────


class TestLoaderDiscovery:
    async def test_loader_discovers_linear(self):
        impl_dir = Path(linear_module.__file__).resolve().parents[1]
        loader = PluginLoader(impl_dir)
        registry = await loader.load_all()
        assert "linear" in registry
        meta = registry["linear"]
        assert any("issue" in c.artifact_types for c in meta.outbounds)
        assert meta.compensates  # has compensate handlers
