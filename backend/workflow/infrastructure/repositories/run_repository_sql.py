"""SqlAlchemyRunRepository — concrete :class:`RunRepository` over one session.

v8 D44/D45. The application layer constructs one instance per request /
worker tick (sharing the session that owns the transaction boundary). All
sqlalchemy concerns live here; callers see only the Protocol.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.workflow.infrastructure.db import ExecutionRun


class SqlAlchemyRunRepository:
    """SQLAlchemy-backed :class:`RunRepository`.

    Constructor-injected with one :class:`AsyncSession`. The session owns the
    transaction; the repository never calls ``commit`` and never opens a new
    transaction. ``add`` defers to ``session.add`` — flush timing is the
    caller's concern (the existing call sites already flush after their
    logical unit).
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, run_id: uuid.UUID) -> ExecutionRun | None:
        return await self._session.get(ExecutionRun, run_id)

    async def list_by_workspace(
        self, workspace_id: uuid.UUID, *, limit: int = 50
    ) -> list[ExecutionRun]:
        stmt = (
            select(ExecutionRun)
            .where(ExecutionRun.workspace_id == workspace_id)
            .order_by(ExecutionRun.created_at.desc())
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def find_by_request_id(self, request_id: uuid.UUID) -> ExecutionRun | None:
        stmt = select(ExecutionRun).where(ExecutionRun.request_id == request_id).limit(1)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def add(self, run: ExecutionRun) -> None:
        self._session.add(run)


__all__ = ["SqlAlchemyRunRepository"]
