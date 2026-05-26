"""SafeModeQueue — founder approval gate for outbound deliveries.

Workflow §12.5 #8 (Bundle G — Delivery) and Workflow §10.5 (Safe Mode).
When the workspace is in Safe Mode, every deliverable lands here instead
of auto-dispatching; the founder approves or denies via the queue UI, and
the dispatcher only runs on approval.

Retention window (Workflow §10.5):

* Initial active window: **90 days** from enqueue
* Per-item extension: **+30 days**, max **2** extensions (so 90 + 30 + 30 = 150 days max)
* After expiry: item flips to ``expired`` status (no further action)
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.delivery.db import SafeModeQueueItemRow, SafeModeStatus

logger = structlog.get_logger(__name__)

INITIAL_TTL_DAYS = 90
EXTENSION_TTL_DAYS = 30
MAX_EXTENSIONS = 2


class SafeModeQueue:
    """Pull-based approval queue for outbound deliveries."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def enqueue(
        self,
        *,
        workspace_id: uuid.UUID,
        deliverable_id: uuid.UUID,
        run_id: uuid.UUID | None = None,
    ) -> uuid.UUID:
        """Enqueue a pending delivery; returns the queue item id.

        ``run_id`` is the optional per-Run grouping key (B12a / Workflow §1.2).
        Existing callers omit it and pre-B12a rows keep working; new callers
        (DeliveryWorker) always thread the originating event's run_id through.
        """
        now = datetime.now(tz=UTC)
        row = SafeModeQueueItemRow(
            id=uuid.uuid4(),
            workspace_id=workspace_id,
            deliverable_id=deliverable_id,
            run_id=run_id,
            status=SafeModeStatus.PENDING,
            expires_at=now + timedelta(days=INITIAL_TTL_DAYS),
            extension_count=0,
            created_at=now,
        )
        self._session.add(row)
        await self._session.flush()
        logger.info(
            "safe_mode_enqueued",
            workspace_id=str(workspace_id),
            deliverable_id=str(deliverable_id),
            item_id=str(row.id),
        )
        return row.id

    async def list_pending(self, *, workspace_id: uuid.UUID) -> list[SafeModeQueueItemRow]:
        """Founder-facing list of items awaiting approval (newest first)."""
        stmt = (
            select(SafeModeQueueItemRow)
            .where(
                SafeModeQueueItemRow.workspace_id == workspace_id,
                SafeModeQueueItemRow.status == SafeModeStatus.PENDING,
            )
            .order_by(SafeModeQueueItemRow.created_at.desc())
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def list_pending_for_run(
        self, *, workspace_id: uuid.UUID, run_id: uuid.UUID
    ) -> list[SafeModeQueueItemRow]:
        """The pending items for one run (B12a) — drives per-Run approve.

        Returned in creation order (oldest first) so dispatch happens in the
        same order the agent loop emitted the artifacts. Empty list when the
        run has no pending items (or never existed)."""
        stmt = (
            select(SafeModeQueueItemRow)
            .where(
                SafeModeQueueItemRow.workspace_id == workspace_id,
                SafeModeQueueItemRow.run_id == run_id,
                SafeModeQueueItemRow.status == SafeModeStatus.PENDING,
            )
            .order_by(SafeModeQueueItemRow.created_at.asc())
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def list_resolved(self, *, workspace_id: uuid.UUID) -> list[SafeModeQueueItemRow]:
        """Founder-facing list of decided items (approved / denied / expired),
        most-recently-decided first. Powers the Decisions "Resolved" tab's
        delivery side; ``decided_at`` is the sort key (created_at as a stable
        tiebreaker for a defensively-undecided row)."""
        stmt = (
            select(SafeModeQueueItemRow)
            .where(
                SafeModeQueueItemRow.workspace_id == workspace_id,
                SafeModeQueueItemRow.status.in_(
                    [
                        SafeModeStatus.APPROVED,
                        SafeModeStatus.DENIED,
                        SafeModeStatus.EXPIRED,
                    ]
                ),
            )
            .order_by(
                SafeModeQueueItemRow.decided_at.desc(),
                SafeModeQueueItemRow.created_at.desc(),
            )
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def approve(
        self,
        *,
        workspace_id: uuid.UUID,
        item_id: uuid.UUID,
        actor_id: uuid.UUID,
    ) -> bool:
        """Flip ``pending → approved``. Returns False if not found / not pending.

        The caller is responsible for handing the deliverable to the
        :class:`backend.delivery.dispatcher.DeliveryDispatcher` AFTER the
        commit succeeds.
        """
        del actor_id  # surface for audit hook (Bundle G integration)
        return await self._transition(
            workspace_id=workspace_id,
            item_id=item_id,
            from_status=SafeModeStatus.PENDING,
            to_status=SafeModeStatus.APPROVED,
        )

    async def deny(
        self,
        *,
        workspace_id: uuid.UUID,
        item_id: uuid.UUID,
        actor_id: uuid.UUID,
        reason: str,
    ) -> bool:
        """Flip ``pending → denied``. Returns False if not found / not pending."""
        del actor_id, reason  # surface for audit hook
        return await self._transition(
            workspace_id=workspace_id,
            item_id=item_id,
            from_status=SafeModeStatus.PENDING,
            to_status=SafeModeStatus.DENIED,
        )

    async def extend(
        self,
        *,
        workspace_id: uuid.UUID,
        item_id: uuid.UUID,
    ) -> bool:
        """Extend the active window by ``EXTENSION_TTL_DAYS``.

        Returns False if not found OR already at ``MAX_EXTENSIONS``.
        """
        row = await self._session.get(SafeModeQueueItemRow, item_id)
        if row is None or row.workspace_id != workspace_id:
            return False
        if row.status not in (SafeModeStatus.PENDING, SafeModeStatus.EXTENDED):
            return False
        if row.extension_count >= MAX_EXTENSIONS:
            return False
        row.extension_count += 1
        row.expires_at = row.expires_at + timedelta(days=EXTENSION_TTL_DAYS)
        row.status = SafeModeStatus.EXTENDED
        await self._session.flush()
        return True

    async def expire(self, *, workspace_id: uuid.UUID) -> int:
        """Sweep pending items past ``expires_at`` to ``expired``. Returns count."""
        now = datetime.now(tz=UTC)
        stmt = (
            update(SafeModeQueueItemRow)
            .where(
                SafeModeQueueItemRow.workspace_id == workspace_id,
                SafeModeQueueItemRow.status.in_([SafeModeStatus.PENDING, SafeModeStatus.EXTENDED]),
                SafeModeQueueItemRow.expires_at <= now,
            )
            .values(status=SafeModeStatus.EXPIRED, decided_at=now)
            .returning(SafeModeQueueItemRow.id)
        )
        result = await self._session.execute(stmt)
        ids = result.scalars().all()
        await self._session.flush()
        if ids:
            logger.info(
                "safe_mode_expired",
                workspace_id=str(workspace_id),
                count=len(ids),
            )
        return len(ids)

    async def _transition(
        self,
        *,
        workspace_id: uuid.UUID,
        item_id: uuid.UUID,
        from_status: SafeModeStatus,
        to_status: SafeModeStatus,
    ) -> bool:
        row = await self._session.get(SafeModeQueueItemRow, item_id)
        if row is None or row.workspace_id != workspace_id:
            return False
        if row.status != from_status:
            return False
        row.status = to_status
        row.decided_at = datetime.now(tz=UTC)
        await self._session.flush()
        return True


__all__ = [
    "EXTENSION_TTL_DAYS",
    "INITIAL_TTL_DAYS",
    "MAX_EXTENSIONS",
    "SafeModeQueue",
]
