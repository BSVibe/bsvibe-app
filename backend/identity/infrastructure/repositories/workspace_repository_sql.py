"""SqlAlchemyWorkspaceRepository — concrete over one AsyncSession.

v8 D44/D45. One instance per request / worker tick (sharing the session
that owns the transaction boundary). All SQLAlchemy concerns live here;
callers see only the Protocol.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.identity.db import MembershipRow
from backend.identity.workspaces_db import WorkspaceRow


class SqlAlchemyWorkspaceRepository:
    """SQLAlchemy-backed :class:`WorkspaceRepository`.

    Constructor-injected with one :class:`AsyncSession`. The session owns
    the transaction; the repository never calls ``commit`` and never opens
    a new transaction. ``add`` defers to ``session.add`` — flush timing is
    the caller's concern.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, workspace_id: uuid.UUID) -> WorkspaceRow | None:
        return await self._session.get(WorkspaceRow, workspace_id)

    async def get_live(self, workspace_id: uuid.UUID) -> WorkspaceRow | None:
        row = await self._session.get(WorkspaceRow, workspace_id)
        if row is None or row.deleted_at is not None:
            return None
        return row

    async def list_for_user(self, user_id: uuid.UUID) -> list[WorkspaceRow]:
        stmt = (
            select(WorkspaceRow)
            .join(MembershipRow, MembershipRow.workspace_id == WorkspaceRow.id)
            .where(
                MembershipRow.user_id == user_id,
                MembershipRow.left_at.is_(None),
                WorkspaceRow.deleted_at.is_(None),
            )
            .order_by(WorkspaceRow.created_at.desc())
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def list_active_regions(self) -> list[tuple[uuid.UUID, str, bool]]:
        stmt = select(WorkspaceRow.id, WorkspaceRow.region, WorkspaceRow.safe_mode).where(
            WorkspaceRow.deleted_at.is_(None)
        )
        result = await self._session.execute(stmt)
        return [(wid, region, safe) for wid, region, safe in result.all()]

    async def add(self, workspace: WorkspaceRow) -> None:
        self._session.add(workspace)


__all__ = ["SqlAlchemyWorkspaceRepository"]
