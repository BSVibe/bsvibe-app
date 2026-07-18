"""IdempotencyRepository Protocol — read/write seam for TriggerEvent intake.

v8 D44/D45. The :class:`backend.workflow.infrastructure.intake.db.TriggerEventRow`
aggregate carries the canonical de-dup key for every inbound trigger
(``(workspace_id, source, idempotency_key)`` — a unique constraint at the
DB layer). Application code — :class:`WebhookReceiver`, :class:`DirectTrigger`,
and the IntakeWorker — calls this Protocol instead of importing the legacy
:mod:`backend.workflow.infrastructure.idempotency` helpers or issuing raw
``select(TriggerEventRow)`` queries.

Method surface limited to what existing callers actually use today: a
duplicate check, an INSERT-with-flush ``record``, and the IntakeWorker's
"give me TriggerEvents that don't yet have a Request" claim query.
"""

from __future__ import annotations

import uuid
from typing import Protocol, runtime_checkable

from backend.workflow.infrastructure.intake.db import TriggerEventRow


@runtime_checkable
class IdempotencyRepository(Protocol):
    """Persistence seam for :class:`TriggerEventRow` (idempotency-keyed intake)."""

    async def is_duplicate(
        self,
        *,
        workspace_id: uuid.UUID,
        source: str,
        idempotency_key: str,
    ) -> bool:
        """``True`` when a row with this ``(workspace_id, source, key)`` triple exists.

        The cheap pre-flight check Webhook / Direct / Schedule receivers run
        before constructing the row; the unique constraint on the table is
        the source of truth (a race is caught at flush time as
        :class:`sqlalchemy.exc.IntegrityError`).
        """

    async def record(self, row: TriggerEventRow, *, producer_id: str) -> None:
        """Write the trigger row through the ``trigger_events`` channel + flush.

        The write is guarded by ``producer_id`` (INV-1 — the only legal write
        path is ``Channel.emit``). Unlike the other Workflow Repositories'
        ``add`` (which only stages), ``record`` flushes — the caller wants the
        ``IntegrityError`` surface synchronously so the race-loser can rollback
        + treat as duplicate. Does NOT commit; the caller owns the transaction
        boundary (v8 D45).
        """

    async def list_undrained(self, *, limit: int = 50) -> list[TriggerEventRow]:
        """Up to ``limit`` TriggerEvents that have no paired ``RequestRow`` yet,
        oldest-first (by ``received_at``).

        Powers :class:`backend.workflow.infrastructure.workers.intake_worker.IntakeWorker._claim_batch`
        — the IntakeWorker turns each into a Request. The concrete impl
        composes ``.with_for_update(skip_locked=True)`` for the row lock; the
        worker filters filter-rejected rows in-process (the
        :data:`RECEIVE_FILTERED_KEY` payload sentinel) on top of the returned
        list.
        """


__all__ = ["IdempotencyRepository"]
