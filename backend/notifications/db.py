"""NotificationPrefs persistence — one row per workspace.

Holds the founder's notification preferences: an events x channels enable
matrix (which moments reach them on which channel) and a quiet-hours window.
There is exactly one row per ``workspace_id`` (resolution is get-or-create:
a workspace with no row reads :data:`DEFAULT_MATRIX` + the default quiet hours,
which are then persisted).

The matrix is stored as a JSON column keyed ``event_id -> channel_id -> bool``.
The event ids (matrix rows) are the fixed :data:`DEFAULT_EVENTS`; the channel
ids (columns) are NOT fixed — they are DERIVED per workspace from its connector
bindings (:func:`backend.notifications.bindings.available_channels`) plus the
always-present ``in_app`` inbox. The matrix read is deliberately tolerant: a
stale channel key (a since-removed connector) is harmless (ignored at send
time), and a newly-bound connector needs no matrix write to become selectable.
Quiet hours are stored as ``"HH:MM"`` strings, the same shape the PWA time
inputs emit, so no minutes-since-midnight conversion is needed on either side.

Follows the model/Base style of :mod:`backend.connectors.db` /
:mod:`backend.router.accounts.account_models`: a single-table declarative model on the
shared :class:`backend.data.Base`, ``workspace_id``-scoped, with a per-module
``<Module>Base`` alias for back-compat.
"""

from __future__ import annotations

import copy
import uuid
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import JSON, Boolean, DateTime, Index, Integer, String, UniqueConstraint
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column

from backend.data import Base

NotificationsBase = Base

# The five notification moments (matrix rows). These ids are the STABLE keys the
# matrix is keyed on — the PWA labels them. The channel COLUMNS are not fixed
# here; they are derived per workspace from connector bindings + ``in_app``.
DEFAULT_EVENTS: tuple[str, ...] = (
    "needs_you",
    "triggered",
    "shipped",
    "failed",
    "daily_brief",
)

# The seed matrix a fresh workspace reads: only the always-present ``in_app``
# inbox is expressed, since a fresh workspace has no connector channels yet.
# A decision waiting on you / an outside trigger / a verified ship / a give-up
# all land in the inbox; the daily brief is a calm digest, off in-app by
# default. Connector channels (slack/telegram/discord/email-sender) appear as
# columns the moment they are bound — the PWA renders them from
# ``available_channels`` and a PUT persists the founder's choice for them.
DEFAULT_MATRIX: dict[str, dict[str, bool]] = {
    "needs_you": {"in_app": True},
    "triggered": {"in_app": True},
    "shipped": {"in_app": True},
    "failed": {"in_app": True},
    "daily_brief": {"in_app": False},
}

DEFAULT_QUIET_HOURS_START = "22:00"
DEFAULT_QUIET_HOURS_END = "08:00"


def default_matrix() -> dict[str, dict[str, bool]]:
    """A fresh deep copy of :data:`DEFAULT_MATRIX` (never share the mutable
    module-level dict across rows)."""
    return copy.deepcopy(DEFAULT_MATRIX)


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


class NotificationPrefsRow(NotificationsBase):
    """A workspace's notification preferences (one row per workspace)."""

    __tablename__ = "notification_prefs"
    __table_args__ = (UniqueConstraint("workspace_id", name="uq_notification_prefs_workspace_id"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    # event_id -> channel_id -> enabled. A small fixed grid; JSON keeps it
    # one column without a child table for a v1 surface.
    matrix: Mapped[dict[str, dict[str, bool]]] = mapped_column(
        JSON, nullable=False, default=default_matrix
    )
    quiet_hours_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    quiet_hours_start: Mapped[str] = mapped_column(
        String(5), nullable=False, default=DEFAULT_QUIET_HOURS_START
    )
    quiet_hours_end: Mapped[str] = mapped_column(
        String(5), nullable=False, default=DEFAULT_QUIET_HOURS_END
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )


class NotificationStatus(StrEnum):
    """Lifecycle of one outbox notification (Notifier N2).

    ``pending`` — queued in the caller's transaction, not yet drained.
    ``sent`` — the NotifyWorker completed the push fan-out (at least one channel
    delivered, OR there were no push channels to send — e.g. quiet hours / an
    in-app-only workspace — which is still a completed, non-error outcome).
    ``failed`` — every attempted push channel raised across the retry budget.
    """

    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"


class NotificationEventRow(NotificationsBase):
    """Durable notification outbox — one row per notification moment (Notifier N2).

    The transactional-outbox row behind the ``notification_outbox``
    :class:`~backend.channels.Channel`. A producer (today: ``create_decision``,
    for the ``needs_you`` event) stages one row in its OWN transaction via
    ``NOTIFICATION_OUTBOX.emit`` — so the notification is confirmed iff the
    triggering write commits (a rolled-back Decision leaves no ghost
    notification), and a crash after commit still leaves the row for the
    :class:`NotifyWorker` to drain (no lost notification). Mirrors the
    :class:`~backend.workflow.infrastructure.delivery.db.DeliveryEventRow` /
    :class:`~plugin.audit.models.AuditOutboxRecord` outbox idiom.

    ``dedupe_key`` is UNIQUE: a double-emit for the same moment (a retried
    ``create_decision``) is a DB-level no-op (``IntegrityError`` → already
    queued), so the founder is notified exactly once per Decision. ``payload``
    carries the channel-agnostic ``{title, body, link, run_id?, decision_id?}``
    the per-connector notify builders shape into each channel's send payload.
    """

    __tablename__ = "notification_events"
    __table_args__ = (
        UniqueConstraint("dedupe_key", name="uq_notification_events_dedupe_key"),
        Index("ix_notification_events_status_created", "status", "created_at"),
        Index("ix_notification_events_workspace", "workspace_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    event: Mapped[str] = mapped_column(String(32), nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[NotificationStatus] = mapped_column(
        SAEnum(
            NotificationStatus,
            name="notification_status_enum",
            values_callable=lambda ec: [m.value for m in ec],
        ),
        nullable=False,
        default=NotificationStatus.PENDING,
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


__all__ = [
    "DEFAULT_EVENTS",
    "DEFAULT_MATRIX",
    "DEFAULT_QUIET_HOURS_END",
    "DEFAULT_QUIET_HOURS_START",
    "NotificationEventRow",
    "NotificationStatus",
    "NotificationPrefsRow",
    "NotificationsBase",
    "default_matrix",
]
