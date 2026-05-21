"""DeliveryDispatcher — plugin outbound fan-out with per-plugin soft-fail."""

from __future__ import annotations

import uuid

import pytest

from backend.delivery.dispatcher import DeliveryDispatcher
from backend.plugins.base import (
    OutboundCapability,
    PluginMeta,
    PluginRunError,
)


def _meta(name: str, *, artifact_types: list[str], outbound_fn) -> PluginMeta:
    """Build a PluginMeta with one OutboundCapability."""
    return PluginMeta(
        name=name,
        version="1.0.0",
        description=f"{name} test plugin",
        author="",
        data_jurisdiction="us",
        credentials=[],
        inbounds=[],
        outbounds=[OutboundCapability(artifact_types=tuple(artifact_types), fn=outbound_fn)],
        actions={},
    )


@pytest.mark.asyncio
async def test_dispatch_to_matching_plugin() -> None:
    captured = {}

    async def slack_send(_ctx, event):
        captured["payload"] = event
        return {"sent_to": "#general"}

    plugin = _meta("slack", artifact_types=["page", "direct_output"], outbound_fn=slack_send)
    disp = DeliveryDispatcher()
    result = await disp.dispatch(
        workspace_id=uuid.uuid4(),
        deliverable_id=uuid.uuid4(),
        artifact_type="direct_output",
        plugins=[plugin],
        event={"text": "hi"},
    )
    assert len(result.actions) == 1
    assert result.actions[0].succeeded is True
    assert result.actions[0].output == {"sent_to": "#general"}
    assert captured["payload"] == {"text": "hi"}
    assert result.error is None


@pytest.mark.asyncio
async def test_non_matching_artifact_type_skipped() -> None:
    async def email_send(_ctx, _event):
        return {"sent": True}

    plugin = _meta("email", artifact_types=["page"], outbound_fn=email_send)
    result = await DeliveryDispatcher().dispatch(
        workspace_id=uuid.uuid4(),
        deliverable_id=uuid.uuid4(),
        artifact_type="pr",  # plugin doesn't subscribe to pr
        plugins=[plugin],
        event={},
    )
    assert result.actions == []


@pytest.mark.asyncio
async def test_one_plugin_failure_does_not_abort_others() -> None:
    async def boom(_ctx, _event):
        raise PluginRunError("upstream 500")

    async def ok(_ctx, _event):
        return {"ok": True}

    bad = _meta("slack", artifact_types=["direct_output"], outbound_fn=boom)
    good = _meta("telegram", artifact_types=["direct_output"], outbound_fn=ok)

    result = await DeliveryDispatcher().dispatch(
        workspace_id=uuid.uuid4(),
        deliverable_id=uuid.uuid4(),
        artifact_type="direct_output",
        plugins=[bad, good],
        event={},
    )
    by_action = {a.action: a for a in result.actions}
    assert by_action["slack:outbound:direct_output"].succeeded is False
    assert "500" in (by_action["slack:outbound:direct_output"].error or "")
    assert by_action["telegram:outbound:direct_output"].succeeded is True
    # At least one succeeded → DeliveryResult.error stays None.
    assert result.error is None


@pytest.mark.asyncio
async def test_all_plugins_fail_sets_error() -> None:
    async def boom1(_ctx, _event):
        raise PluginRunError("fail-1")

    async def boom2(_ctx, _event):
        raise PluginRunError("fail-2")

    plugins = [
        _meta("a", artifact_types=["pr"], outbound_fn=boom1),
        _meta("b", artifact_types=["pr"], outbound_fn=boom2),
    ]
    result = await DeliveryDispatcher().dispatch(
        workspace_id=uuid.uuid4(),
        deliverable_id=uuid.uuid4(),
        artifact_type="pr",
        plugins=plugins,
        event={},
    )
    assert all(a.succeeded is False for a in result.actions)
    assert result.error == "fail-2"  # last error wins


@pytest.mark.asyncio
async def test_empty_plugin_list_returns_empty_actions() -> None:
    result = await DeliveryDispatcher().dispatch(
        workspace_id=uuid.uuid4(),
        deliverable_id=uuid.uuid4(),
        artifact_type="pr",
        plugins=[],
        event={},
    )
    assert result.actions == []
    assert result.error is None
