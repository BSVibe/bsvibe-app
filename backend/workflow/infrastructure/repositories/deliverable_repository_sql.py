"""SqlAlchemyDeliverableRepository — concrete :class:`DeliverableRepository`.

v8 D44/D45. One instance per request / worker tick, sharing the session that
owns the transaction boundary. All sqlalchemy concerns live here; callers
see only the Protocol.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.workflow.infrastructure.db import Deliverable


class SqlAlchemyDeliverableRepository:
    """SQLAlchemy-backed :class:`DeliverableRepository`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, deliverable_id: uuid.UUID) -> Deliverable | None:
        return await self._session.get(Deliverable, deliverable_id)

    async def list_by_workspace(
        self,
        workspace_id: uuid.UUID,
        *,
        run_id: uuid.UUID | None = None,
        limit: int = 50,
    ) -> list[Deliverable]:
        stmt = select(Deliverable).where(Deliverable.workspace_id == workspace_id)
        if run_id is not None:
            stmt = stmt.where(Deliverable.run_id == run_id)
        stmt = stmt.order_by(Deliverable.created_at.desc()).limit(limit)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def list_by_run(self, run_id: uuid.UUID, workspace_id: uuid.UUID) -> list[Deliverable]:
        stmt = (
            select(Deliverable)
            .where(
                Deliverable.run_id == run_id,
                Deliverable.workspace_id == workspace_id,
            )
            .order_by(Deliverable.created_at.asc())
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def list_by_run_id(self, run_id: uuid.UUID) -> list[Deliverable]:
        stmt = select(Deliverable).where(Deliverable.run_id == run_id)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def find_first_by_run(self, run_id: uuid.UUID) -> Deliverable | None:
        stmt = select(Deliverable).where(Deliverable.run_id == run_id).limit(1)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def add(self, deliverable: Deliverable) -> None:
        self._session.add(deliverable)


__all__ = ["SqlAlchemyDeliverableRepository"]
