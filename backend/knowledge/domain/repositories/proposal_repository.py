"""ProposalRepository Protocol — read/write seam for canonicalization proposals.

v8 D44/D45. Knowledge's canonicalization queue lives in PG (the
``canonicalization_proposals`` table); listing the table returned the queue
view BSage's Safe-Mode approval surface reads from. Application code (REST
handlers, canonicalization service, promotion pipeline) calls this Protocol
instead of issuing raw ``select(CanonicalizationProposal)`` queries.

Concrete impl: :mod:`backend.knowledge.infrastructure.repositories.proposal_repository_sql`.
"""

from __future__ import annotations

import uuid
from typing import Protocol, runtime_checkable

from backend.knowledge.canonicalization.db import (
    CanonicalizationProposal,
    ProposalStatus,
)


@runtime_checkable
class ProposalRepository(Protocol):
    """Persistence seam for :class:`CanonicalizationProposal` rows."""

    async def get(self, proposal_id: uuid.UUID) -> CanonicalizationProposal | None:
        """Return the proposal with this id, or ``None`` if it doesn't exist."""

    async def list_by_workspace(
        self, workspace_id: uuid.UUID, *, limit: int = 100
    ) -> list[CanonicalizationProposal]:
        """Return proposals in this workspace, newest-first (created_at desc)."""

    async def list_pending_by_workspace(
        self, workspace_id: uuid.UUID, *, limit: int = 100
    ) -> list[CanonicalizationProposal]:
        """Return pending proposals in this workspace, oldest-first (queue order).

        The Safe Mode approval surface reads this — it's the queue the founder
        works through, so FIFO ordering is the natural default.
        """

    async def list_by_status(
        self,
        workspace_id: uuid.UUID,
        status: ProposalStatus,
        *,
        limit: int = 100,
    ) -> list[CanonicalizationProposal]:
        """Return proposals in this workspace + status (created_at desc)."""

    async def add(self, proposal: CanonicalizationProposal) -> None:
        """Stage a new proposal for INSERT on the next flush.

        The repository does NOT flush or commit — transaction boundaries are
        owned at the application service / request scope (v8 D45).
        """


__all__ = ["ProposalRepository"]
