"""Tests for backend.plugins.decorator — capability decorator API per Workflow §6 #4."""

from __future__ import annotations

import pytest

from backend.plugins import (
    ActionCapability,
    InboundCapability,
    OutboundCapability,
    PluginMeta,
    PluginRegistrationError,
    plugin,
)


def _make_plugin(**overrides):
    kwargs = {
        "name": "github",
        "credentials": [{"name": "token", "required": True}],
        "data_jurisdiction": "us",
    }
    kwargs.update(overrides)
    return plugin(**kwargs)


class TestPluginFactory:
    def test_returns_builder_with_meta(self):
        p = _make_plugin()
        assert isinstance(p.meta, PluginMeta)
        assert p.meta.name == "github"
        assert p.meta.data_jurisdiction == "us"
        assert p.meta.credentials == [{"name": "token", "required": True}]

    def test_defaults_version_description_author(self):
        p = _make_plugin()
        assert p.meta.version == "0.1.0"
        assert p.meta.description == ""
        assert p.meta.author == ""

    def test_rejects_missing_data_jurisdiction(self):
        with pytest.raises(TypeError):
            plugin(name="github", credentials=[])  # type: ignore[call-arg]

    def test_rejects_invalid_data_jurisdiction(self):
        with pytest.raises(PluginRegistrationError, match="data_jurisdiction"):
            plugin(name="github", credentials=[], data_jurisdiction="atlantis")

    def test_accepts_known_jurisdictions(self):
        for j in ("us", "eu", "kr", "local", "unknown"):
            p = plugin(name=f"plugin-{j}", credentials=[], data_jurisdiction=j)
            assert p.meta.data_jurisdiction == j

    def test_rejects_invalid_name(self):
        with pytest.raises(PluginRegistrationError, match="name"):
            plugin(name="Bad_Name", credentials=[], data_jurisdiction="us")
        with pytest.raises(PluginRegistrationError, match="name"):
            plugin(name="9starts", credentials=[], data_jurisdiction="us")

    def test_accepts_valid_name_with_hyphens(self):
        p = plugin(name="github-pr", credentials=[], data_jurisdiction="us")
        assert p.meta.name == "github-pr"


class TestInboundDecorator:
    def test_registers_inbound_capability(self):
        p = _make_plugin()

        @p.inbound(trigger={"type": "webhook"})
        async def on_webhook(context, payload):  # pragma: no cover
            return None

        assert len(p.meta.inbounds) == 1
        cap = p.meta.inbounds[0]
        assert isinstance(cap, InboundCapability)
        assert cap.fn is on_webhook
        assert cap.trigger == {"type": "webhook"}

    def test_multiple_inbounds_allowed(self):
        p = _make_plugin()

        @p.inbound(trigger={"type": "webhook"})
        async def f1(context, payload):  # pragma: no cover
            return None

        @p.inbound(trigger={"type": "cron", "schedule": "0 * * * *"})
        async def f2(context, payload):  # pragma: no cover
            return None

        assert len(p.meta.inbounds) == 2

    def test_rejects_invalid_trigger_type(self):
        p = _make_plugin()
        with pytest.raises(PluginRegistrationError, match="trigger"):

            @p.inbound(trigger={"type": "telepathy"})
            async def f(context, payload):  # pragma: no cover
                return None

    def test_accepts_all_known_trigger_types(self):
        for t in ("cron", "webhook", "on_input", "write_event", "on_demand", "on_deliver"):
            p = _make_plugin(name=f"p-{t.replace('_', '-')}")

            @p.inbound(trigger={"type": t})
            async def f(context, payload):  # pragma: no cover
                return None

            assert p.meta.inbounds[0].trigger["type"] == t

    def test_rejects_missing_trigger_type(self):
        p = _make_plugin()
        with pytest.raises(PluginRegistrationError, match="trigger"):

            @p.inbound(trigger={})
            async def f(context, payload):  # pragma: no cover
                return None


class TestOutboundDecorator:
    def test_registers_outbound_with_artifact_types(self):
        p = _make_plugin()

        @p.outbound(artifact_types=["code", "pr"])
        async def deliver_pr(context, event):  # pragma: no cover
            return None

        assert len(p.meta.outbounds) == 1
        cap = p.meta.outbounds[0]
        assert isinstance(cap, OutboundCapability)
        assert cap.artifact_types == ("code", "pr")
        assert cap.fn is deliver_pr

    def test_multiple_outbounds_with_disjoint_types(self):
        p = _make_plugin()

        @p.outbound(artifact_types=["code", "pr"])
        async def f1(context, event):  # pragma: no cover
            return None

        @p.outbound(artifact_types=["issue_comment"])
        async def f2(context, event):  # pragma: no cover
            return None

        assert len(p.meta.outbounds) == 2

    def test_rejects_overlapping_artifact_types(self):
        p = _make_plugin()

        @p.outbound(artifact_types=["code", "pr"])
        async def f1(context, event):  # pragma: no cover
            return None

        with pytest.raises(PluginRegistrationError, match="artifact_type"):

            @p.outbound(artifact_types=["pr", "tag"])
            async def f2(context, event):  # pragma: no cover
                return None

    def test_rejects_empty_artifact_types(self):
        p = _make_plugin()
        with pytest.raises(PluginRegistrationError, match="artifact_types"):

            @p.outbound(artifact_types=[])
            async def f(context, event):  # pragma: no cover
                return None


class TestActionDecorator:
    def test_registers_action_by_name(self):
        p = _make_plugin()

        @p.action(name="open_pr", mcp_exposed=True)
        async def open_pr(context, branch, title, body):  # pragma: no cover
            return {}

        assert "open_pr" in p.meta.actions
        cap = p.meta.actions["open_pr"]
        assert isinstance(cap, ActionCapability)
        assert cap.name == "open_pr"
        assert cap.mcp_exposed is True
        assert cap.fn is open_pr

    def test_mcp_exposed_defaults_false(self):
        p = _make_plugin()

        @p.action(name="local_only")
        async def f(context):  # pragma: no cover
            return {}

        assert p.meta.actions["local_only"].mcp_exposed is False

    def test_rejects_duplicate_action_name(self):
        p = _make_plugin()

        @p.action(name="open_pr")
        async def f1(context):  # pragma: no cover
            return {}

        with pytest.raises(PluginRegistrationError, match="action"):

            @p.action(name="open_pr")
            async def f2(context):  # pragma: no cover
                return {}


class TestSetupDecorator:
    def test_registers_setup_fn(self):
        p = _make_plugin()

        @p.setup
        async def setup(cred_store):  # pragma: no cover
            return {}

        assert p.meta.setup_fn is setup

    def test_rejects_duplicate_setup(self):
        p = _make_plugin()

        @p.setup
        async def s1(cred_store):  # pragma: no cover
            return {}

        with pytest.raises(PluginRegistrationError, match="setup"):

            @p.setup
            async def s2(cred_store):  # pragma: no cover
                return {}


class TestNoCategoryNoNotify:
    """Workflow §6 #4: category and @execute.notify are dropped."""

    def test_meta_has_no_category_field(self):
        p = _make_plugin()
        assert not hasattr(p.meta, "category")

    def test_plugin_kwarg_category_rejected(self):
        with pytest.raises(TypeError):
            plugin(  # type: ignore[call-arg]
                name="x", credentials=[], data_jurisdiction="us", category="input"
            )

    def test_no_notify_decorator_on_inbound_function(self):
        p = _make_plugin()

        @p.inbound(trigger={"type": "webhook"})
        async def on_webhook(context, payload):  # pragma: no cover
            return None

        assert not hasattr(on_webhook, "notify")
