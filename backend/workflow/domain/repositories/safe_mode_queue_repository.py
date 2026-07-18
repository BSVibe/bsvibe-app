"""SafeModeQueueRepository Protocol — read/write seam for the SafeModeQueueItem aggregate.

v8 D44/D45. The Safe Mode queue (Workflow §10.5) is the founder approval gate
for outbound deliveries. The lifecycle has rich domain semantics
(``pending → approved → delivered → archived → deleted``, plus
``pending → denied`` / ``expired`` / ``extended``), and the existing
:class:`backend.workflow.application.safe_mode_queue.SafeModeQueue` service
already owns those transitions.

This Protocol is the **persistence seam beneath** that service: it captures
the raw read/write surface (``get``, ``list_*``, ``enqueue``, ``mark_expired_bulk``)
so the service no longer issues raw ``select(SafeModeQueueItemRow)`` /
``session.get(SafeModeQueueItemRow, ...)`` queries. Lifecycle methods
(``approve``/``deny``/``mark_delivered``/``extend``/etc.) stay on the
:class:`SafeModeQueue` service as domain operations — the Repository just
loads the row and the service mutates it. Per v8 D44, that is the correct
split: ``Repository = persistence`` vs ``service = orchestration``.

Method surface limited to what existing callers actually use.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Protocol, runtime_checkable

from backend.workflow.infrastructure.delivery.db import SafeModeQueueItemRow


@runtime_checkable
class SafeModeQueueRepository(Protocol):
    """Persistence seam for :class:`SafeModeQueueItemRow` rows."""

    async def get(self, item_id: uuid.UUID) -> SafeModeQueueItemRow | None:
        """Return the queue item with this id, or ``None`` if it doesn't exist."""

    async def list_pending_by_workspace(
        self, workspace_id: uuid.UUID
    ) -> list[SafeModeQueueItemRow]:
        """PENDING items in this workspace, newest-first.

        Powers ``GET /api/v1/safemode/queue`` — the founder's "things waiting
        for me to approve" inbox.
        """

    async def list_pending_for_run(
        self, *, workspace_id: uuid.UUID, run_id: uuid.UUID
    ) -> list[SafeModeQueueItemRow]:
        """PENDING items for one run (B12a), oldest-first by ``created_at``.

        Drives per-Run approve — dispatched in the same order the agent loop
        emitted the artifacts. Empty list when the run has no pending items.
        """

    async def list_resolved_by_workspace(
        self, workspace_id: uuid.UUID
    ) -> list[SafeModeQueueItemRow]:
        """RESOLVED items (approved / denied / expired), most-recently-decided
        first. Powers the Decisions "Resolved" tab's delivery side."""

    async def list_due_expired(self, *, now: datetime | None = None) -> list[SafeModeQueueItemRow]:
        """Every PENDING / EXTENDED row past ``expires_at`` across ALL workspaces.

        System-wide read (no workspace filter) — D3a / M1 plug-in for
        :class:`SafeModeExpirySweepRunner`, which transitions each returned
        row to ``EXPIRED`` and emits ONE audit row for the batch.
        """

    async def mark_expired_bulk(self, *, workspace_id: uuid.UUID, now: datetime) -> int:
        """Single-statement bulk transition of every PENDING / EXTENDED row past
        ``expires_at`` in one workspace to EXPIRED. Returns the count.

        Per-workspace path with no audit emission — the D3a sweep uses the
        per-row :meth:`SafeModeQueue.mark_expired` for glass-box auditing.
        """

    async def enqueue(self, item: SafeModeQueueItemRow, *, producer_id: str) -> None:
        """Emit a held delivery onto the ``safe_mode_queue_items`` channel (INV-1).

        The write goes through ``SAFE_MODE_QUEUE_ITEMS.emit``, which asserts
        ``producer_id`` is a declared producer before staging the row. The
        repository does NOT flush or commit; the caller owns the transaction
        boundary (v8 D45).
        """


__all__ = ["SafeModeQueueRepository"]
