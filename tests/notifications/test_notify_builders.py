"""NOTIFY_EVENT_BUILDERS — the seam that defines "notification channel" (N1a).

N1a only needs the builder KEYS to exist (so channel derivation can tell a
notify channel from a deliberate seam); the shaping bodies ship in N2. These
tests pin the seam: exactly the four notify connectors are registered, the keys
match the connector ``name=`` (``email-sender``, not ``email``), the non-notify
connectors are absent, and calling a placeholder builder raises rather than
returning a fake payload.
"""

from __future__ import annotations

import pytest

from backend.notifications.notify_builders import (
    NOTIFY_EVENT_BUILDERS,
    NotificationContent,
    ShapedNotification,
)


def test_notify_builders_are_exactly_the_four_notify_connectors() -> None:
    assert set(NOTIFY_EVENT_BUILDERS.keys()) == {
        "slack",
        "telegram",
        "discord",
        "email-sender",
    }


def test_email_connector_key_matches_plugin_name_not_bare_email() -> None:
    # The plugin name is ``email-sender``; a bare ``email`` key would never match
    # a ``connector_accounts.connector`` value, so the channel would be invisible.
    assert "email-sender" in NOTIFY_EVENT_BUILDERS
    assert "email" not in NOTIFY_EVENT_BUILDERS


@pytest.mark.parametrize("seam", ["notion", "linear", "trello", "github", "sentry"])
def test_non_notify_connectors_are_a_deliberate_seam(seam: str) -> None:
    assert seam not in NOTIFY_EVENT_BUILDERS


def test_placeholder_builder_raises_until_n2_shaping_lands() -> None:
    content = NotificationContent(event="needs_you", title="t", body="b", link=None)
    for builder in NOTIFY_EVENT_BUILDERS.values():
        with pytest.raises(NotImplementedError):
            builder(content, {})


def test_shaped_notification_shape() -> None:
    shaped = ShapedNotification(payload={"text": "hi"})
    assert shaped.payload == {"text": "hi"}
    assert shaped.credential_key == "token"
    assert shaped.extra_credentials == {}


def test_notification_content_carries_event_title_body_link() -> None:
    content = NotificationContent(
        event="needs_you", title="Decision", body="A run is waiting", link="/decisions"
    )
    assert content.event == "needs_you"
    assert content.title == "Decision"
    assert content.body == "A run is waiting"
    assert content.link == "/decisions"
