"""NotifyWorker — drain the notification outbox and deliver push channels.

Notifier N2 (the core). The producer (``create_decision``) stages a
``needs_you`` :class:`~backend.notifications.db.NotificationEventRow` in the
Decision's transaction; this worker drains it and actually CALLS the founder on
their configured push channels. It mirrors the
:class:`~backend.workflow.infrastructure.workers.delivery_worker.DeliveryWorker`
outbox-drain shape:

1. Claim a batch of ``pending`` rows under ``SELECT … FOR UPDATE SKIP LOCKED``
   (:func:`build_notify_claim_stmt`) so two server instances draining the same
   queue never double-notify. The lock releases when the batch's status
   update commits.
2. For each row, evaluate the workspace's notification-prefs matrix + quiet
   hours (in the workspace's IANA ``timezone``) to decide which PUSH channels
   the founder wants for this event. ``in_app`` is intentionally NOT sent here
   — the Decision already surfaces in the Brief / live-events inbox; the worker
   only fans out the push channels.
3. Deliver each enabled push channel through its connector binding, DIRECTLY —
   NOT through Safe Mode / ``DeliveryEventRow`` (a founder notification is not
   an outbound-to-the-world delivery, so it is not genuine Safe-Mode risk;
   Notifier §D2). Per-channel SOFT-FAIL: one channel raising never stops the
   others or wedges the queue (mirrors ``ConnectorDispatch``'s per-plugin
   soft-fail).

Quiet hours (Notifier §D5): ``needs_you`` IGNORES quiet hours for the in-app
inbox (which the worker never sends anyway) and SUPPRESSES only the push
channels during the window. So a ``needs_you`` inside quiet hours ⇒ push
suppressed, in-app inbox unaffected; the row still settles to ``sent`` (a
completed, non-error outcome).

Retry: a row whose every attempted push channel failed is left ``pending`` and
retried next tick until ``max_attempts``, then flipped to ``failed`` so a
permanently-misconfigured channel does not spin forever.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, time, tzinfo
from typing import Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import structlog
from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.channels import Channel
from backend.identity.workspaces_db import WorkspaceRow
from backend.notifications.bindings import IN_APP_CHANNEL, resolve_notify_bindings
from backend.notifications.channels import NOTIFICATION_OUTBOX
from backend.notifications.db import (
    DEFAULT_MATRIX,
    NotificationEventRow,
    NotificationPrefsRow,
    NotificationStatus,
)
from backend.notifications.notify_builders import NotificationContent
from backend.workers.base import BaseWorker

logger = structlog.get_logger(__name__)


class NotifySender(Protocol):
    """Port the worker calls to actually deliver ONE push channel.

    The concrete implementation (:class:`~backend.workflow.application.runtime.notify_runtime.PluginNotifySender`)
    shapes the notification via ``NOTIFY_EVENT_BUILDERS[connector]``, decrypts
    the account secret, and dispatches the connector's ``@p.outbound``. Defined
    here (port-where-used) so the worker depends on the interface, not the
    plugin machinery — a test double satisfies it structurally.
    """

    async def send(
        self,
        *,
        connector: str,
        content: NotificationContent,
        delivery_config: dict[str, object],
        signing_secret_ciphertext: str,
    ) -> None: ...


def _parse_hhmm(value: str) -> time | None:
    """Parse a ``"HH:MM"`` quiet-hours bound; ``None`` on a malformed value."""
    try:
        hh, mm = value.split(":", 1)
        return time(hour=int(hh), minute=int(mm))
    except (ValueError, TypeError):
        return None


def within_quiet_hours(now_local: time, start: str, end: str) -> bool:
    """Is ``now_local`` inside the ``[start, end)`` quiet-hours window?

    Handles the wrap-around case (e.g. ``22:00``–``08:00`` spanning midnight):
    when ``start <= end`` the window is the simple interval; when ``start > end``
    the window is "at or after start, OR before end". A malformed bound disables
    the window (returns ``False``) rather than guessing.
    """
    s, e = _parse_hhmm(start), _parse_hhmm(end)
    if s is None or e is None or s == e:
        return False
    if s < e:
        return s <= now_local < e
    return now_local >= s or now_local < e


def _workspace_now(timezone: str) -> time:
    """Current local wall-clock time for ``timezone`` (falls back to UTC)."""
    tz: tzinfo
    try:
        tz = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        tz = UTC
    return datetime.now(tz=tz).timetz().replace(tzinfo=None)


def build_notify_claim_stmt(*, batch_size: int) -> Select[tuple[NotificationEventRow]]:
    """Multi-server safe claim of pending notification rows (``FOR UPDATE SKIP LOCKED``).

    Extracted as a builder so a unit test can pin that the rendered SQL carries
    the lock hint — the load-bearing guard against two workers double-notifying.
    """
    return (
        select(NotificationEventRow)
        .where(NotificationEventRow.status == NotificationStatus.PENDING)
        .order_by(NotificationEventRow.created_at.asc())
        .limit(batch_size)
        .with_for_update(skip_locked=True)
    )


@dataclass(slots=True)
class NotifyWorkerConfig:
    batch_size: int = 50
    poll_interval_s: float = 5.0
    max_attempts: int = 5


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


class NotifyWorker(BaseWorker):
    """Periodic drain of ``notification_outbox`` into the founder's push channels."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        sender: NotifySender,
        config: NotifyWorkerConfig | None = None,
    ) -> None:
        self._cfg = config or NotifyWorkerConfig()
        super().__init__(name="notify_worker", poll_interval_s=self._cfg.poll_interval_s)
        self._session_factory = session_factory
        self._sender = sender

    async def _tick(self) -> int:
        return await self.drain_once()

    async def drain_once(self) -> int:
        """Claim a batch of pending rows, deliver each, commit the status updates."""
        async with self._session_factory() as session:
            stmt = build_notify_claim_stmt(batch_size=self._cfg.batch_size)

            async def _claim() -> list[NotificationEventRow]:
                return list((await session.execute(stmt)).scalars().all())

            rows = await self._channel().consume(
                consumer_id="worker:notify_worker",
                claim=_claim,
            )
            if not rows:
                return 0
            processed = 0
            for row in rows:
                await self._deliver_row(session, row)
                processed += 1
            await session.commit()
            return processed

    @staticmethod
    def _channel() -> Channel[NotificationEventRow]:
        return NOTIFICATION_OUTBOX

    async def _deliver_row(self, session: AsyncSession, row: NotificationEventRow) -> None:
        """Evaluate prefs + quiet hours for one row and fan out its push channels."""
        matrix = await self._matrix(session, row.workspace_id)
        enabled = {
            ch for ch, on in matrix.get(row.event, {}).items() if on and ch != IN_APP_CHANNEL
        }

        # Quiet hours suppress ONLY push channels; the in-app inbox is unaffected
        # (the Decision already surfaces there — the worker never sends in_app).
        if enabled and await self._in_quiet_hours(session, row.workspace_id):
            logger.info(
                "notify_quiet_hours_push_suppressed",
                workspace_id=str(row.workspace_id),
                notify_event=row.event,
            )
            enabled = set()

        # Only channels that are BOTH matrix-enabled AND actually bound as a
        # notify channel for this workspace (available_channels ∩ enabled).
        bindings = await resolve_notify_bindings(session, workspace_id=row.workspace_id)
        binding_by_connector = {b.connector: b for b in bindings}
        targets = [c for c in enabled if c in binding_by_connector]

        content = self._content(row)
        succeeded = 0
        for connector in targets:
            binding = binding_by_connector[connector]
            try:
                await self._sender.send(
                    connector=connector,
                    content=content,
                    delivery_config=dict(binding.account.delivery_config),
                    signing_secret_ciphertext=binding.account.signing_secret_ciphertext,
                )
                succeeded += 1
                logger.info(
                    "notify_channel_sent",
                    workspace_id=str(row.workspace_id),
                    notify_event=row.event,
                    connector=connector,
                )
            except Exception:  # noqa: BLE001 — per-channel soft-fail, never wedge the queue
                logger.exception(
                    "notify_channel_failed",
                    workspace_id=str(row.workspace_id),
                    notify_event=row.event,
                    connector=connector,
                )

        self._settle(row, attempted=len(targets), succeeded=succeeded)

    def _settle(self, row: NotificationEventRow, *, attempted: int, succeeded: int) -> None:
        """Move the row to a terminal state after a fan-out pass.

        No push channels to attempt (in-app-only / quiet hours / no bound
        channel) OR at least one channel delivered ⇒ ``sent`` (a completed,
        non-error outcome). Every attempted channel failed ⇒ retry until the
        attempt budget, then ``failed``.
        """
        if attempted == 0 or succeeded > 0:
            row.status = NotificationStatus.SENT
            row.sent_at = _utcnow()
            return
        row.attempts += 1
        if row.attempts >= self._cfg.max_attempts:
            row.status = NotificationStatus.FAILED
            logger.warning(
                "notify_row_failed",
                workspace_id=str(row.workspace_id),
                notify_event=row.event,
                attempts=row.attempts,
            )
        # else: leave PENDING — the next tick retries the failed channels.

    @staticmethod
    def _content(row: NotificationEventRow) -> NotificationContent:
        payload = row.payload or {}
        return NotificationContent(
            event=row.event,
            title=str(payload.get("title") or ""),
            body=str(payload.get("body") or ""),
            link=(str(payload["link"]) if payload.get("link") else None),
        )

    @staticmethod
    async def _matrix(session: AsyncSession, workspace_id: uuid.UUID) -> dict[str, dict[str, bool]]:
        """The workspace's prefs matrix (its own, or the default seed matrix)."""
        prefs = (
            await session.execute(
                select(NotificationPrefsRow).where(
                    NotificationPrefsRow.workspace_id == workspace_id
                )
            )
        ).scalar_one_or_none()
        return prefs.matrix if prefs is not None else DEFAULT_MATRIX

    @staticmethod
    async def _in_quiet_hours(session: AsyncSession, workspace_id: uuid.UUID) -> bool:
        prefs = (
            await session.execute(
                select(NotificationPrefsRow).where(
                    NotificationPrefsRow.workspace_id == workspace_id
                )
            )
        ).scalar_one_or_none()
        if prefs is None or not prefs.quiet_hours_enabled:
            return False
        workspace = await session.get(WorkspaceRow, workspace_id)
        timezone = workspace.timezone if workspace is not None else "UTC"
        return within_quiet_hours(
            _workspace_now(timezone), prefs.quiet_hours_start, prefs.quiet_hours_end
        )


__all__ = [
    "NotifySender",
    "NotifyWorker",
    "NotifyWorkerConfig",
    "build_notify_claim_stmt",
    "within_quiet_hours",
]
