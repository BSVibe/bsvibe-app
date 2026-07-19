"""Per-connector notification shaping — the seam that defines "notify channel".

Mirrors :mod:`backend.workflow.application.delivery.connector_dispatch._builders`
(``OUTBOUND_EVENT_BUILDERS``): one builder per connector turns a
:class:`NotificationContent` (the channel-agnostic ``{event, title, body,
link}``) plus the connector's stable ``delivery_config`` into a
:class:`ShapedNotification` (the per-channel-shaped payload + which credential
slot the decrypted account secret lands under).

:data:`NOTIFY_EVENT_BUILDERS` IS the single source of truth for which connectors
count as notification channels: a connector with no notify builder is a
deliberate seam (not a notify channel) — notion / linear / trello / github /
sentry deliver *work outward* but are not places the founder is *notified*, so
they are absent here. Adding a key here (and its plugin) makes that connector a
notify channel everywhere the channel model is derived — no capability flag is
restated on the catalog.

N1a scope: only the KEYS need to exist so channel-derivation
(:func:`backend.notifications.bindings.resolve_notify_bindings`) can tell a
notify channel from a seam. The per-channel shaping itself is implemented in
N2 (when real delivery ships); until then the builder body raises so no caller
mistakes the seam for a working sender.
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
    """The channel-ready notification: the send payload + credential routing.

    Mirrors ``ShapedEvent`` in the outbound builders. ``payload`` is the
    per-channel-shaped body the plugin action consumes; ``credential_key`` is
    the slot the decrypted per-account secret is injected under (channels read
    their token from different keys); ``extra_credentials`` carries any
    additional non-secret slots (sourced from ``delivery_config``).
    """

    payload: dict[str, Any]
    credential_key: str = "token"
    extra_credentials: dict[str, str] = field(default_factory=dict)


# A builder maps {notification content} + {connector delivery_config} → a
# ShapedNotification. Same shape as ``OutboundEventBuilder``.
NotifyBuilder = Callable[[NotificationContent, dict[str, Any]], ShapedNotification]


def _unimplemented_builder(
    content: NotificationContent, delivery_config: dict[str, Any]
) -> ShapedNotification:
    """N1a placeholder — the KEY defines the seam; shaping ships in N2.

    N1a only needs :data:`NOTIFY_EVENT_BUILDERS` keys to exist so notify-channel
    membership is derivable. Real per-channel shaping (slack blocks, telegram
    markdown, discord embeds, email HTML) lands with the sender in N2; calling a
    builder now is a wiring bug, so it raises rather than return a fake payload.
    """
    raise NotImplementedError(
        "notify shaping is implemented in N2; N1a only derives channel membership"
    )


# The notify seam. Keys MUST match the plugin ``name=`` (and the
# ``connector_accounts.connector`` value) so binding resolution lines up — note
# the email connector's name is ``email-sender``, not ``email``. A connector
# absent here is not a notification channel (a deliberate seam).
NOTIFY_EVENT_BUILDERS: dict[str, NotifyBuilder] = {
    "slack": _unimplemented_builder,
    "telegram": _unimplemented_builder,
    "discord": _unimplemented_builder,
    "email-sender": _unimplemented_builder,
}


__all__ = [
    "NOTIFY_EVENT_BUILDERS",
    "NotificationContent",
    "NotifyBuilder",
    "ShapedNotification",
]
