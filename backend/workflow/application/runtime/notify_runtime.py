"""Notification push-delivery runtime (Notifier N2).

The concrete :class:`~backend.workflow.infrastructure.workers.notify_worker.NotifySender`
that turns one notification + one connector binding into a real send, plus the
:func:`build_notify_sender` factory the worker bootstrap wires.

It reuses the delivery path's primitives WITHOUT going through
``ConnectorDispatch.dispatch()`` / Safe Mode / ``DeliveryEventRow`` (Notifier
§D2): it shapes the notification via ``NOTIFY_EVENT_BUILDERS[connector]``,
decrypts the per-account secret with :class:`CredentialCipher`, borrows the
same no-op-LLM :class:`SkillContext` the outbound delivery uses
(:func:`_build_context`), and dispatches the connector's existing
``@p.outbound`` through the shared :class:`PluginRunner`. So a notification
travels the connector's real sender — there is no second delivery code path,
and the founder-facing credential is decrypted only in-memory here, never
logged (python-security).
"""

from __future__ import annotations

import structlog

from backend.extensions.plugin.base import PluginMeta
from backend.extensions.plugin.runner import PluginRunner
from backend.notifications.notify_builders import NOTIFY_EVENT_BUILDERS, NotificationContent
from backend.router.accounts.crypto import CredentialCipher
from backend.workflow.application.delivery.connector_dispatch._context import _build_context

logger = structlog.get_logger(__name__)


class PluginNotifySender:
    """Deliver ONE notification push channel through its connector ``@p.outbound``.

    Implements :class:`~backend.workflow.infrastructure.workers.notify_worker.NotifySender`.
    Raising here (a missing builder/plugin, a misconfigured target from the
    builder, or a plugin send failure) is a PER-CHANNEL soft-fail: the worker
    catches it and moves on to the next channel without wedging the queue.
    """

    def __init__(
        self,
        *,
        plugins_by_name: dict[str, PluginMeta],
        cipher: CredentialCipher,
        runner: PluginRunner | None = None,
    ) -> None:
        self._plugins = plugins_by_name
        self._cipher = cipher
        self._runner = runner or PluginRunner()

    async def send(
        self,
        *,
        connector: str,
        content: NotificationContent,
        delivery_config: dict[str, object],
        signing_secret_ciphertext: str,
    ) -> None:
        builder = NOTIFY_EVENT_BUILDERS.get(connector)
        plugin = self._plugins.get(connector)
        if builder is None or plugin is None:
            raise RuntimeError(f"no notify channel for connector {connector!r}")
        # May raise ValueError for a misconfigured target (missing chat_id /
        # channel / to) — surfaced as this channel's soft-fail by the worker.
        shaped = builder(content, dict(delivery_config))
        credentials: dict[str, str] = {
            shaped.credential_key: self._cipher.decrypt(signing_secret_ciphertext)
        }
        credentials.update(shaped.extra_credentials)
        context = _build_context(credentials=credentials, config=dict(delivery_config))
        await self._runner.dispatch_outbound(
            plugin,
            artifact_type=shaped.artifact_type,
            context=context,
            event=shaped.payload,
        )
        # NOTE: never log ``credentials`` / the decrypted secret (python-security).


def build_notify_sender(
    *,
    plugins: list[PluginMeta],
    cipher: CredentialCipher,
) -> PluginNotifySender:
    """Wrap the loaded plugins + a cipher into a worker-facing notify sender."""
    return PluginNotifySender(
        plugins_by_name={p.name: p for p in plugins},
        cipher=cipher,
    )


__all__ = ["PluginNotifySender", "build_notify_sender"]
