"""SqlAlchemyDecisionRepository — concrete :class:`DecisionRepository`."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.workflow.infrastructure.db import Decision, DecisionStatus


class SqlAlchemyDecisionRepository:
    """SQLAlchemy-backed :class:`DecisionRepository`.

    Constructor-injected with one :class:`AsyncSession`. The session owns the
    transaction; this repository never commits and never opens a new one.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, decision_id: uuid.UUID) -> Decision | None:
        return await self._session.get(Decision, decision_id)

    async def list_pending_by_workspace(self, workspace_id: uuid.UUID) -> list[Decision]:
        stmt = (
            select(Decision)
            .where(
                Decision.workspace_id == workspace_id,
                Decision.status == DecisionStatus.PENDING,
            )
            .order_by(Decision.created_at.desc())
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def list_resolved_by_workspace(self, workspace_id: uuid.UUID) -> list[Decision]:
        stmt = (
            select(Decision)
            .where(
                Decision.workspace_id == workspace_id,
                Decision.status == DecisionStatus.RESOLVED,
            )
            .order_by(Decision.resolved_at.desc(), Decision.created_at.desc())
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def list_by_run(self, run_id: uuid.UUID, workspace_id: uuid.UUID) -> list[Decision]:
        stmt = (
            select(Decision)
            .where(
                Decision.run_id == run_id,
                Decision.workspace_id == workspace_id,
            )
            .order_by(Decision.created_at.desc())
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def add(self, decision: Decision) -> None:
        self._session.add(decision)


__all__ = ["SqlAlchemyDecisionRepository"]
