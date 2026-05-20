"""Tests for backend.plugins.runner — dispatch by capability per Workflow §6 #4."""

from __future__ import annotations

from typing import Any

import pytest

from backend.plugins import PluginRunError, PluginRunner, plugin


def _make_runner() -> PluginRunner:
    return PluginRunner(credential_store=None, event_bus=None)


class _Ctx:
    """Minimal context for plugin execution in tests."""

    def __init__(self, **kwargs: Any) -> None:
        self.credentials: dict[str, Any] = kwargs.get("credentials", {})
        self.input_data: dict[str, Any] | None = kwargs.get("input_data")
        self.config: dict[str, Any] = kwargs.get("config", {})


@pytest.fixture
def github_plugin():
    p = plugin(name="github", credentials=[], data_jurisdiction="us")

    @p.inbound(trigger={"type": "webhook"})
    async def on_webhook(context, payload):
        return {"received": payload.get("id")}

    @p.outbound(artifact_types=["code", "pr"])
    async def deliver_pr(context, event):
        return {"artifact": "pr", "id": event.get("id")}

    @p.outbound(artifact_types=["issue_comment"])
    async def deliver_comment(context, event):
        return {"artifact": "comment"}

    @p.action(name="open_pr", mcp_exposed=True)
    async def open_pr(context, branch, title, body):
        return {"opened": True, "branch": branch}

    return p


class TestDispatchInbound:
    async def test_calls_inbound_for_matching_trigger_type(self, github_plugin):
        runner = _make_runner()
        result = await runner.dispatch_inbound(
            github_plugin.meta,
            trigger_type="webhook",
            context=_Ctx(),
            payload={"id": 42},
        )
        assert result == {"received": 42}

    async def test_raises_when_no_matching_inbound(self, github_plugin):
        runner = _make_runner()
        with pytest.raises(PluginRunError, match="inbound"):
            await runner.dispatch_inbound(
                github_plugin.meta,
                trigger_type="cron",
                context=_Ctx(),
                payload={},
            )


class TestDispatchOutbound:
    async def test_routes_by_artifact_type(self, github_plugin):
        runner = _make_runner()
        result = await runner.dispatch_outbound(
            github_plugin.meta,
            artifact_type="pr",
            context=_Ctx(),
            event={"id": "evt-1"},
        )
        assert result == {"artifact": "pr", "id": "evt-1"}

    async def test_routes_second_outbound_by_artifact_type(self, github_plugin):
        runner = _make_runner()
        result = await runner.dispatch_outbound(
            github_plugin.meta,
            artifact_type="issue_comment",
            context=_Ctx(),
            event={},
        )
        assert result == {"artifact": "comment"}

    async def test_raises_when_unknown_artifact_type(self, github_plugin):
        runner = _make_runner()
        with pytest.raises(PluginRunError, match="artifact_type"):
            await runner.dispatch_outbound(
                github_plugin.meta,
                artifact_type="rocketship",
                context=_Ctx(),
                event={},
            )


class TestDispatchAction:
    async def test_invokes_action_by_name(self, github_plugin):
        runner = _make_runner()
        result = await runner.dispatch_action(
            github_plugin.meta,
            action_name="open_pr",
            context=_Ctx(),
            kwargs={"branch": "main", "title": "t", "body": "b"},
        )
        assert result == {"opened": True, "branch": "main"}

    async def test_raises_when_unknown_action(self, github_plugin):
        runner = _make_runner()
        with pytest.raises(PluginRunError, match="action"):
            await runner.dispatch_action(
                github_plugin.meta,
                action_name="nuke",
                context=_Ctx(),
                kwargs={},
            )


class TestErrorWrapping:
    async def test_wraps_plugin_exception_into_plugin_run_error(self):
        p = plugin(name="boom", credentials=[], data_jurisdiction="local")

        @p.outbound(artifact_types=["thing"])
        async def deliver(context, event):
            raise RuntimeError("kaboom")

        runner = _make_runner()
        with pytest.raises(PluginRunError, match="kaboom"):
            await runner.dispatch_outbound(
                p.meta,
                artifact_type="thing",
                context=_Ctx(),
                event={},
            )


class TestInputSchemaValidation:
    async def test_validates_input_against_action_schema(self):
        p = plugin(name="schema-plug", credentials=[], data_jurisdiction="local")

        @p.action(
            name="add",
            input_schema={
                "type": "object",
                "required": ["a", "b"],
                "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
                "additionalProperties": False,
            },
        )
        async def add(context, a, b):
            return {"sum": a + b}

        runner = _make_runner()
        result = await runner.dispatch_action(
            p.meta, action_name="add", context=_Ctx(), kwargs={"a": 1, "b": 2}
        )
        assert result == {"sum": 3}

        with pytest.raises(PluginRunError, match="schema"):
            await runner.dispatch_action(
                p.meta, action_name="add", context=_Ctx(), kwargs={"a": "no", "b": 2}
            )
