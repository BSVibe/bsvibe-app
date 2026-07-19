"""Per-connector notification shaping — the seam that defines "notify channel".

Mirrors :mod:`backend.workflow.application.delivery.connector_dispatch._builders`
(``OUTBOUND_EVENT_BUILDERS``): one builder per connector turns a
:class:`NotificationContent` (the channel-agnostic ``{event, title, body,
link}``) plus the connector's stable ``delivery_config`` into a
:class:`ShapedNotification` (the ``artifact_type`` to dispatch the connector's
``@p.outbound`` under, the per-channel-shaped send payload, and which
credential slot the decrypted account secret lands under).

:data:`NOTIFY_EVENT_BUILDERS` IS the single source of truth for which connectors
count as notification channels: a connector with no notify builder is a
deliberate seam (not a notify channel) — notion / linear / trello / github /
sentry deliver *work outward* but are not places the founder is *notified*, so
they are absent here. Adding a key here (and its plugin) makes that connector a
notify channel everywhere the channel model is derived — no capability flag is
restated on the catalog.

The send payload each builder emits is the SAME shape the connector's existing
``@p.outbound`` handler consumes (slack ``{channel, text}``, telegram
``{chat_id, text}``, discord ``{channel_id, content}``, email-sender ``{to,
subject, body, as_text}``), so the NotifyWorker dispatches a notification
through the connector's real sender — no second delivery path. Routing (the
``chat_id`` / ``channel`` / ``channel_id`` / ``to`` target) comes from the
stable founder-set ``delivery_config`` (config-not-content, like the outbound
builders), never from the notification text; a missing target is a
misconfigured channel → ``ValueError`` (surfaced as a per-channel soft-fail by
the worker, never wedging the queue).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class NotificationContent:
    """The channel-agnostic notification to be shaped for one channel.

    ``event`` is one of :data:`backend.notifications.db.DEFAULT_EVENTS`;
    ``title`` / ``body`` are the human-facing text; ``link`` is an optional
    deep link (e.g. the PWA Decisions URL) a channel may render as an action.
    """

    event: str
    title: str
    body: str
    link: str | None = None


@dataclass(frozen=True, slots=True)
class ShapedNotification:
    """The channel-ready notification: dispatch target + send payload + credential.

    Mirrors ``ShapedEvent`` in the outbound builders. ``artifact_type`` is the
    connector ``@p.outbound`` artifact_type the send payload is dispatched under
    (e.g. ``telegram_message``); ``payload`` is the per-channel-shaped body that
    outbound consumes; ``credential_key`` is the slot the decrypted per-account
    secret is injected under (channels read their token from different keys);
    ``extra_credentials`` carries any additional non-secret slots (sourced from
    ``delivery_config``).
    """

    artifact_type: str
    payload: dict[str, Any]
    credential_key: str = "token"
    extra_credentials: dict[str, str] = field(default_factory=dict)


# A builder maps {notification content} + {connector delivery_config} → a
# ShapedNotification. Same shape as ``OutboundEventBuilder``.
NotifyBuilder = Callable[[NotificationContent, dict[str, Any]], ShapedNotification]


def _message_text(content: NotificationContent) -> str:
    """Flatten the notification into a single message string for chat channels.

    Chat channels (slack/telegram/discord) take one text/content field, so the
    title, body and (optional) link are joined into one block — title first so
    it reads as a heading, the link last as a trailing call to action. Empty
    sections are dropped so a body-less notification is not padded with blank
    lines.
    """
    parts = [content.title.strip(), content.body.strip()]
    if content.link:
        parts.append(content.link.strip())
    return "\n\n".join(p for p in parts if p)


def build_slack_notification(
    content: NotificationContent, delivery_config: dict[str, Any]
) -> ShapedNotification:
    """Shape a notification into slack's ``deliver_message`` payload (``{channel, text}``).

    ``channel`` is routing from the stable ``delivery_config``; a missing one is
    a misconfigured channel → ``ValueError``. The decrypted account secret is
    injected as ``bot_token`` (the slot slack's ``_client`` reads).
    """
    channel = delivery_config.get("channel")
    if not channel:
        raise ValueError("slack notify delivery_config missing required 'channel'")
    return ShapedNotification(
        artifact_type="slack_message",
        payload={"channel": str(channel), "text": _message_text(content)},
        credential_key="bot_token",
    )


def build_telegram_notification(
    content: NotificationContent, delivery_config: dict[str, Any]
) -> ShapedNotification:
    """Shape a notification into telegram's ``deliver_message`` payload (``{chat_id, text}``).

    ``chat_id`` is routing from the stable ``delivery_config``; a missing one is
    a misconfigured channel → ``ValueError``. The decrypted account secret is
    injected as ``bot_token``.
    """
    chat_id = delivery_config.get("chat_id")
    if not chat_id:
        raise ValueError("telegram notify delivery_config missing required 'chat_id'")
    return ShapedNotification(
        artifact_type="telegram_message",
        payload={"chat_id": str(chat_id), "text": _message_text(content)},
        credential_key="bot_token",
    )


def build_discord_notification(
    content: NotificationContent, delivery_config: dict[str, Any]
) -> ShapedNotification:
    """Shape a notification into discord's ``deliver_message`` payload (``{channel_id, content}``).

    ``channel_id`` is routing from the stable ``delivery_config``; a missing one
    is a misconfigured channel → ``ValueError``. The decrypted account secret is
    injected as ``bot_token``.
    """
    channel_id = delivery_config.get("channel_id")
    if not channel_id:
        raise ValueError("discord notify delivery_config missing required 'channel_id'")
    return ShapedNotification(
        artifact_type="discord_message",
        payload={"channel_id": str(channel_id), "content": _message_text(content)},
        credential_key="bot_token",
    )


def build_email_notification(
    content: NotificationContent, delivery_config: dict[str, Any]
) -> ShapedNotification:
    """Shape a notification into email-sender's ``deliver_email`` payload.

    * ``to`` — routing from the stable ``delivery_config``; a missing one is a
      misconfigured channel → ``ValueError``.
    * ``from`` — optional founder-set sender override; omitted when unset so the
      plugin falls back to its own ``email_from`` config.
    * ``subject`` — the notification title.
    * ``body`` — the notification body, with the deep link appended when present
      (sent as plain text via ``as_text``).

    The decrypted account secret is injected as ``api_key``.
    """
    to = delivery_config.get("to")
    if not to:
        raise ValueError("email notify delivery_config missing required 'to'")
    body = content.body.strip()
    if content.link:
        body = f"{body}\n\n{content.link.strip()}" if body else content.link.strip()
    payload: dict[str, Any] = {
        "to": str(to),
        "subject": content.title.strip() or "BSVibe notification",
        "body": body,
        "as_text": True,
    }
    sender = delivery_config.get("from")
    if sender:
        payload["from"] = str(sender)
    return ShapedNotification(
        artifact_type="email",
        payload=payload,
        credential_key="api_key",
    )


# The notify seam. Keys MUST match the plugin ``name=`` (and the
# ``connector_accounts.connector`` value) so binding resolution lines up — note
# the email connector's name is ``email-sender``, not ``email``. A connector
# absent here is not a notification channel (a deliberate seam).
NOTIFY_EVENT_BUILDERS: dict[str, NotifyBuilder] = {
    "slack": build_slack_notification,
    "telegram": build_telegram_notification,
    "discord": build_discord_notification,
    "email-sender": build_email_notification,
}


__all__ = [
    "NOTIFY_EVENT_BUILDERS",
    "NotificationContent",
    "NotifyBuilder",
    "ShapedNotification",
    "build_discord_notification",
    "build_email_notification",
    "build_slack_notification",
    "build_telegram_notification",
]
