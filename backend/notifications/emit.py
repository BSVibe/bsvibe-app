"""Shared notification-outbox emit — the one savepoint-wrapped producer seam.

Every notification producer stages its outbox row through
:func:`emit_notification`, so the dedupe-savepoint logic lives in ONE place
instead of being copy-pasted at each producer site:

* ``create_decision`` → ``needs_you`` (a run stopped on a Decision),
* the ``IntakeWorker`` → ``triggered`` (an external/autonomous trigger started work),
* ``write_verified_deliverable`` → ``shipped`` (a verified deliverable shipped),
* ``AgentRunner.transition`` → ``failed`` (a run reached its FAILED terminal).

The row is written through ``NOTIFICATION_OUTBOX.emit`` (the only legal producer
path — a bare ``session.add`` is forbidden by the INV-1 guard) inside a SAVEPOINT.
The UNIQUE ``dedupe_key`` makes a re-emit of the same moment a DB-level no-op
(``IntegrityError`` → already queued): the nested transaction rolls back only the
duplicate outbox insert, leaving the triggering write (already flushed BEFORE this
call) intact and the founder notified exactly once per moment.

Staged in the SAME transaction/session as the triggering terminal write, so the
notification is confirmed iff that write commits (a rolled-back write leaves no
ghost notification), and a crash after commit still leaves the row for the
NotifyWorker to drain (no lost notification). Direct SEND stays with the worker —
never inside a producer's write path (Notifier §D3).
"""

from __future__ import annotations

import uuid

import structlog
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.notifications.channels import NOTIFICATION_OUTBOX
from backend.notifications.db import NotificationEventRow

logger = structlog.get_logger(__name__)


async def emit_notification(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    event: str,
    dedupe_key: str,
    payload: dict[str, object],
    producer_id: str,
) -> None:
    """Stage one notification-outbox row in the caller's transaction.

    See the module docstring for the transactional + dedupe contract. A
    duplicate ``dedupe_key`` is swallowed (logged, not raised), so a retried
    producer never double-notifies and never breaks its own terminal write.
    """
    row = NotificationEventRow(
        workspace_id=workspace_id,
        event=event,
        dedupe_key=dedupe_key,
        payload=payload,
    )
    try:
        async with session.begin_nested():
            NOTIFICATION_OUTBOX.emit(session, row, producer_id=producer_id)
            await session.flush()
    except IntegrityError:
        logger.info(
            "notification_outbox_duplicate_skipped",
            notify_event=event,
            dedupe_key=dedupe_key,
            workspace_id=str(workspace_id),
        )


__all__ = ["emit_notification"]
