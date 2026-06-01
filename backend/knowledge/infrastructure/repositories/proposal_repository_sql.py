"""SqlAlchemyProposalRepository — concrete :class:`ProposalRepository` over one session.

v8 D44/D45. The application layer constructs one instance per request /
worker tick (sharing the session that owns the transaction boundary). All
SQLAlchemy concerns live here; callers see only the Protocol.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.knowledge.canonicalization.db import (
    CanonicalizationProposal,
    ProposalStatus,
)


class SqlAlchemyProposalRepository:
    """SQLAlchemy-backed :class:`ProposalRepository`.

    Constructor-injected with one :class:`AsyncSession`. The session owns the
    transaction; the repository never calls ``commit`` and never opens a new
    transaction. ``add`` defers to ``session.add`` — flush timing is the
    caller's concern.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, proposal_id: uuid.UUID) -> CanonicalizationProposal | None:
        return await self._session.get(CanonicalizationProposal, proposal_id)

    async def list_by_workspace(
        self, workspace_id: uuid.UUID, *, limit: int = 100
    ) -> list[CanonicalizationProposal]:
        stmt = (
            select(CanonicalizationProposal)
            .where(CanonicalizationProposal.workspace_id == workspace_id)
            .order_by(CanonicalizationProposal.created_at.desc())
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def list_pending_by_workspace(
        self, workspace_id: uuid.UUID, *, limit: int = 100
    ) -> list[CanonicalizationProposal]:
        stmt = (
            select(CanonicalizationProposal)
            .where(
                CanonicalizationProposal.workspace_id == workspace_id,
                CanonicalizationProposal.status == ProposalStatus.PENDING,
            )
            .order_by(CanonicalizationProposal.created_at.asc())
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def list_by_status(
        self,
        workspace_id: uuid.UUID,
        status: ProposalStatus,
        *,
        limit: int = 100,
    ) -> list[CanonicalizationProposal]:
        stmt = (
            select(CanonicalizationProposal)
            .where(
                CanonicalizationProposal.workspace_id == workspace_id,
                CanonicalizationProposal.status == status,
            )
            .order_by(CanonicalizationProposal.created_at.desc())
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def add(self, proposal: CanonicalizationProposal) -> None:
        self._session.add(proposal)


__all__ = ["SqlAlchemyProposalRepository"]
