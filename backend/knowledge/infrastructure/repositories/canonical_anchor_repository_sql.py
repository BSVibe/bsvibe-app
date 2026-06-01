"""SqlAlchemyCanonicalAnchorRepository — concrete over one AsyncSession.

v8 D44/D45. The application layer constructs one instance per request /
worker tick (sharing the session that owns the transaction boundary). All
SQLAlchemy concerns live here; callers see only the Protocol.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.knowledge.canonicalization.db import CanonicalAnchor


class SqlAlchemyCanonicalAnchorRepository:
    """SQLAlchemy-backed :class:`CanonicalAnchorRepository`.

    Constructor-injected with one :class:`AsyncSession`. The session owns the
    transaction; the repository never calls ``commit`` and never opens a new
    transaction. ``add`` defers to ``session.add`` — flush timing is the
    caller's concern.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, anchor_id: uuid.UUID) -> CanonicalAnchor | None:
        return await self._session.get(CanonicalAnchor, anchor_id)

    async def find_by_name(self, workspace_id: uuid.UUID, name: str) -> CanonicalAnchor | None:
        stmt = (
            select(CanonicalAnchor)
            .where(
                CanonicalAnchor.workspace_id == workspace_id,
                CanonicalAnchor.name == name,
            )
            .limit(1)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_by_workspace(
        self, workspace_id: uuid.UUID, *, limit: int | None = None
    ) -> list[CanonicalAnchor]:
        stmt = (
            select(CanonicalAnchor)
            .where(CanonicalAnchor.workspace_id == workspace_id)
            .order_by(CanonicalAnchor.name.asc())
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def add(self, anchor: CanonicalAnchor) -> None:
        self._session.add(anchor)


__all__ = ["SqlAlchemyCanonicalAnchorRepository"]
