"""SqlAlchemyWorkspaceScheduleRepository — concrete over one AsyncSession.

Lift I-Repo-Final Phase B. Concrete impl of
:class:`~backend.schedule.domain.repositories.workspace_schedule_repository.WorkspaceScheduleRepository`
backed by SQLAlchemy. One instance per worker tick (sharing the session
that owns the transaction boundary). All SQLAlchemy concerns live here;
the runner sees only the Protocol.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.schedule.infrastructure.schedule_db import WorkspaceScheduleRow


class SqlAlchemyWorkspaceScheduleRepository:
    """SQLAlchemy-backed :class:`WorkspaceScheduleRepository`.

    Constructor-injected with one :class:`AsyncSession`. The session owns
    the transaction; the repository never calls ``commit`` and never opens
    a new transaction.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def claim_due(self, *, now: datetime) -> list[WorkspaceScheduleRow]:
        """Select every due, enabled schedule for this tick.

        ``with_for_update(skip_locked=True)`` matters under multi-worker
        prod (a second worker that lands on the same row at the same time
        skips it rather than blocking, and the next tick catches it). On
        SQLite the ``skip_locked`` hint is a no-op (the dialect ignores
        it), which is fine for tests that drive ``fire_due_once`` from a
        single coroutine.
        """
        stmt = (
            select(WorkspaceScheduleRow)
            .where(
                WorkspaceScheduleRow.enabled.is_(True),
                WorkspaceScheduleRow.next_run_at <= now,
            )
            .order_by(WorkspaceScheduleRow.next_run_at.asc())
            .with_for_update(skip_locked=True)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def advance(
        self,
        row: WorkspaceScheduleRow,
        *,
        next_run_at: datetime,
        last_fired_at: datetime,
    ) -> WorkspaceScheduleRow:
        """Flip ``row``'s ``next_run_at`` + ``last_fired_at`` and flush.

        Flushes so the new ``next_run_at`` is visible in the same
        transaction (the runner's per-tick window).
        """
        row.next_run_at = next_run_at
        row.last_fired_at = last_fired_at
        await self._session.flush()
        return row


__all__ = ["SqlAlchemyWorkspaceScheduleRepository"]
