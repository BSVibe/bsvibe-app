"""SqlAlchemySafeModeQueueRepository — concrete :class:`SafeModeQueueRepository`.

v8 D44/D45. The :class:`SafeModeQueue` application service constructs one
of these per its session. The Repository is the raw persistence seam
(``get`` / ``list_*`` / ``add`` / ``mark_expired_bulk``); the service owns
the rich lifecycle transitions on the returned rows.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.workflow.infrastructure.db import ExecutionRun, RunStatus
from backend.workflow.infrastructure.delivery.db import (
    SafeModeQueueItemRow,
    SafeModeStatus,
)


class SqlAlchemySafeModeQueueRepository:
    """SQLAlchemy-backed :class:`SafeModeQueueRepository`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, item_id: uuid.UUID) -> SafeModeQueueItemRow | None:
        return await self._session.get(SafeModeQueueItemRow, item_id)

    async def list_pending_by_workspace(
        self, workspace_id: uuid.UUID
    ) -> list[SafeModeQueueItemRow]:
        # Defensive: a run that reached ``shipped`` has nothing left to
        # approve, so its held item must not surface in the founder-facing
        # "Needs you" queue. Normally the approve path resolves the item
        # before the run ships, but a stale/legacy ship path can leave a
        # ``pending`` item behind a ``shipped`` run — that item would then
        # show up as an already-shipped deliverable begging for approval
        # (founder confusion + double-approval risk). Exclude items whose
        # run is shipped; keep items with no run_id (legacy single-emit).
        shipped_run_ids = select(ExecutionRun.id).where(
            ExecutionRun.workspace_id == workspace_id,
            ExecutionRun.status == RunStatus.SHIPPED,
        )
        stmt = (
            select(SafeModeQueueItemRow)
            .where(
                SafeModeQueueItemRow.workspace_id == workspace_id,
                SafeModeQueueItemRow.status == SafeModeStatus.PENDING,
                or_(
                    SafeModeQueueItemRow.run_id.is_(None),
                    SafeModeQueueItemRow.run_id.not_in(shipped_run_ids),
                ),
            )
            .order_by(SafeModeQueueItemRow.created_at.desc())
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def list_pending_for_run(
        self, *, workspace_id: uuid.UUID, run_id: uuid.UUID
    ) -> list[SafeModeQueueItemRow]:
        stmt = (
            select(SafeModeQueueItemRow)
            .where(
                SafeModeQueueItemRow.workspace_id == workspace_id,
                SafeModeQueueItemRow.run_id == run_id,
                SafeModeQueueItemRow.status == SafeModeStatus.PENDING,
            )
            .order_by(SafeModeQueueItemRow.created_at.asc())
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def list_resolved_by_workspace(
        self, workspace_id: uuid.UUID
    ) -> list[SafeModeQueueItemRow]:
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
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def list_due_expired(self, *, now: datetime | None = None) -> list[SafeModeQueueItemRow]:
        cutoff = now or datetime.now(tz=UTC)
        stmt = (
            select(SafeModeQueueItemRow)
            .where(
                SafeModeQueueItemRow.status.in_([SafeModeStatus.PENDING, SafeModeStatus.EXTENDED]),
                SafeModeQueueItemRow.expires_at <= cutoff,
            )
            .order_by(SafeModeQueueItemRow.expires_at.asc())
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def mark_expired_bulk(self, *, workspace_id: uuid.UUID, now: datetime) -> int:
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
        return len(ids)

    async def add(self, item: SafeModeQueueItemRow) -> None:
        self._session.add(item)


__all__ = ["SqlAlchemySafeModeQueueRepository"]
