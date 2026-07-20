"""Notifications-context channel declaration (INV-1).

Declares the ``notification_outbox`` channel next to its row
(:class:`~backend.notifications.db.NotificationEventRow`). Imports only the
notifications-context row + the generic :class:`~backend.channels.Channel`
core — no cross-context imports, so ``backend.notifications`` stays a common
leaf (the import-linter "common leaves do not import bounded contexts"
contract) while still declaring its coupling as a typed object.

``notification_outbox`` is the founder-notification transactional-outbox queue
(Notifier N2/N3). Its producers each stage one :class:`NotificationEventRow`
via the shared :func:`~backend.notifications.emit.emit_notification` seam. The
four terminal-write producers stage their row inside the SAME transaction as
the write they notify about, so the notification is confirmed iff its
triggering write commits; the fifth (``daily_brief``) is a standalone digest
job with no triggering write (its own commit is the only gate):

* :func:`~backend.workflow.application.run_persistence.create_decision`
  (``workflow:create_decision``) → ``needs_you`` — every path a run stops on a
  Decision flows through it.
* :class:`~backend.workflow.infrastructure.workers.intake_worker.IntakeWorker`
  (``worker:intake_worker``) → ``triggered`` — an external/autonomous trigger
  (webhook / schedule tick) minting a Request; a founder-initiated DIRECT run
  does NOT notify (the founder started it).
* :func:`~backend.workflow.domain.verified_deliverable.write_verified_deliverable`
  (``workflow:verified_deliverable``) → ``shipped`` — the verified terminal ships.
* :meth:`~backend.workflow.application.agent_runner.AgentRunner.transition`
  (``workflow:run_failed``) → ``failed`` — a run reaches its FAILED terminal.
* :class:`~backend.workflow.infrastructure.workers.daily_brief_worker.DailyBriefWorker`
  (``worker:daily_brief``) → ``daily_brief`` — a per-workspace once-a-day digest
  (counts of the last 24h's shipped/failed runs + currently-pending decisions),
  emitted at the workspace's local morning. Unlike the four above it has no
  single triggering write — it is a dedicated digest job (a poll-loop worker),
  deduped on ``daily_brief:<workspace_id>:<local_date>`` for exactly-once-per-day.

Its sole worker-claim consumer is the
:class:`~backend.workflow.infrastructure.workers.notify_worker.NotifyWorker`
(id ``worker:notify_worker``), which claims a batch under ``FOR UPDATE SKIP
LOCKED``, evaluates the workspace's notification-prefs matrix + quiet hours,
and delivers the enabled push channels through the workspace's connector
bindings — directly, NOT through Safe Mode / ``DeliveryEventRow`` (a
notification to the founder is not an outbound-to-the-world delivery, so it is
not genuine Safe-Mode risk; §D2 of the Notifier handoff).

The row is machine-emitted (a terminal-write side effect), so
``human_origin=False`` and there is no authoring surface. Registering the
channel in :mod:`backend.channels.registry` arms the INV-1 producer guard,
which forbids a bare ``session.add(NotificationEventRow(...))`` anywhere in
production code — the ``NOTIFICATION_OUTBOX.emit`` seam becomes the only legal
write, so the producer can never be silently forgotten or bypassed.
"""

from __future__ import annotations

from backend.channels import Channel
from backend.notifications.db import NotificationEventRow

NOTIFICATION_OUTBOX: Channel[NotificationEventRow] = Channel(
    name="notification_outbox",
    row=NotificationEventRow,
    producers=(
        "workflow:create_decision",
        "worker:intake_worker",
        "workflow:verified_deliverable",
        "workflow:run_failed",
        "worker:daily_brief",
    ),
    consumers=("worker:notify_worker",),
    human_origin=False,
)

__all__ = ["NOTIFICATION_OUTBOX"]
