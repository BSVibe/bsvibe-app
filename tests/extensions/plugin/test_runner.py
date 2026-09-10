"""Tests for backend.extensions.plugin.runner — dispatch by capability per Workflow §6 #4."""

from __future__ import annotations

from typing import Any

import pytest

from backend.extensions.plugin import PluginRunError, PluginRunner, plugin


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
    p = plugin(name="github", credentials=[])

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
        p = plugin(name="boom", credentials=[])

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
        p = plugin(name="schema-plug", credentials=[])

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


class TestRunnerDoesNotLeakTokenFromPluginError:
    """H4 — a plugin action that raises a Telegram-style error must not leak the
    bot token through ``PluginRunner._call``, which fans ``str(exc)`` out to the
    log, the wrapped ``PluginRunError``, the run's ``ActionResult``, and a 502.
    The Telegram client scrubs its token at the source, so the message the runner
    wraps is already clean."""

    async def test_wrapped_error_has_no_token(self):
        from plugin.telegram.client import TelegramApiError

        token = "999888:SUPER-SECRET"

        p = plugin(name="tg", credentials=[])

        @p.action(name="send")
        async def send(context):  # noqa: ARG001
            # What the Telegram client raises AFTER scrubbing its own token.
            raise TelegramApiError(
                "Server error '500' for url 'https://api.telegram.org/bot<redacted>/send'"
            )

        runner = _make_runner()
        with pytest.raises(PluginRunError) as caught:
            await runner.dispatch_action(p.meta, action_name="send", context=_Ctx(), kwargs={})
        assert token not in str(caught.value)
        assert "<redacted>" in str(caught.value)
