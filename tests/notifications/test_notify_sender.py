"""PluginNotifySender — shape + decrypt + dispatch one push channel (N2).

The sender is the concrete :class:`NotifySender`: it shapes the notification via
``NOTIFY_EVENT_BUILDERS[connector]``, decrypts the account secret into the slot
that connector's ``_client`` reads, and dispatches the connector's existing
``@p.outbound`` — no second delivery path, no Safe Mode. These tests use a fake
runner + fake cipher so no real plugin / network / KMS is touched.
"""

from __future__ import annotations

from typing import Any

import pytest

from backend.notifications.notify_builders import NotificationContent
from backend.workflow.application.runtime.notify_runtime import (
    PluginNotifySender,
    build_notify_sender,
)

_CONTENT = NotificationContent(
    event="needs_you",
    title="A run needs your decision",
    body="Postgres or SQLite?",
    link="/decisions",
)


class _FakeRunner:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def dispatch_outbound(
        self, plugin: Any, *, artifact_type: str, context: Any, event: Any
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "plugin": plugin,
                "artifact_type": artifact_type,
                "context": context,
                "event": event,
            }
        )
        return {}


class _FakeCipher:
    def decrypt(self, token: str) -> str:
        return f"SECRET::{token}"


async def test_send_decrypts_into_channel_slot_and_dispatches_outbound() -> None:
    runner = _FakeRunner()
    plugin = object()  # the runner is faked, so the plugin is an opaque sentinel
    sender = PluginNotifySender(
        plugins_by_name={"telegram": plugin},  # type: ignore[dict-item]
        cipher=_FakeCipher(),  # type: ignore[arg-type]
        runner=runner,  # type: ignore[arg-type]
    )

    await sender.send(
        connector="telegram",
        content=_CONTENT,
        delivery_config={"chat_id": "42"},
        signing_secret_ciphertext="CT",
    )

    assert len(runner.calls) == 1
    call = runner.calls[0]
    assert call["plugin"] is plugin
    assert call["artifact_type"] == "telegram_message"
    assert call["event"]["chat_id"] == "42"
    assert "Postgres or SQLite?" in call["event"]["text"]
    # The decrypted secret lands under the telegram credential slot — and is
    # only ever in-memory here (never logged).
    assert call["context"].credentials["bot_token"] == "SECRET::CT"


async def test_send_unknown_connector_raises() -> None:
    sender = PluginNotifySender(
        plugins_by_name={},
        cipher=_FakeCipher(),
        runner=_FakeRunner(),  # type: ignore[arg-type]
    )
    with pytest.raises(RuntimeError):
        await sender.send(
            connector="telegram",
            content=_CONTENT,
            delivery_config={"chat_id": "42"},
            signing_secret_ciphertext="CT",
        )


async def test_send_misconfigured_target_raises_valueerror_for_soft_fail() -> None:
    # A missing routing target bubbles the builder's ValueError so the worker
    # soft-fails this channel (never sending to a default target).
    runner = _FakeRunner()
    sender = PluginNotifySender(
        plugins_by_name={"telegram": object()},  # type: ignore[dict-item]
        cipher=_FakeCipher(),  # type: ignore[arg-type]
        runner=runner,  # type: ignore[arg-type]
    )
    with pytest.raises(ValueError):
        await sender.send(
            connector="telegram",
            content=_CONTENT,
            delivery_config={},
            signing_secret_ciphertext="CT",
        )
    assert runner.calls == []


def test_build_notify_sender_indexes_plugins_by_name() -> None:
    class _P:
        def __init__(self, name: str) -> None:
            self.name = name

    sender = build_notify_sender(
        plugins=[_P("telegram"), _P("slack")],  # type: ignore[list-item]
        cipher=_FakeCipher(),  # type: ignore[arg-type]
    )
    assert set(sender._plugins) == {"telegram", "slack"}
