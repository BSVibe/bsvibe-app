"""Knowledge Repository concrete implementations.

v8 D44/D45. Lift I-Repo-Knowledge ships:

* :class:`VaultNoteRepository` — :class:`~backend.knowledge.domain.repositories.NoteRepository`
  over :class:`~backend.knowledge.graph.storage.StorageBackend`.
* :class:`SqlAlchemyProposalRepository` — :class:`~backend.knowledge.domain.repositories.ProposalRepository`
  over one :class:`~sqlalchemy.ext.asyncio.AsyncSession`.
* :class:`SqlAlchemyCanonicalAnchorRepository` — :class:`~backend.knowledge.domain.repositories.CanonicalAnchorRepository`
  over one :class:`~sqlalchemy.ext.asyncio.AsyncSession`.
"""

from __future__ import annotations

from backend.knowledge.infrastructure.repositories.canonical_anchor_repository_sql import (
    SqlAlchemyCanonicalAnchorRepository,
)
from backend.knowledge.infrastructure.repositories.note_repository_vault import (
    VaultNoteRepository,
)
from backend.knowledge.infrastructure.repositories.proposal_repository_sql import (
    SqlAlchemyProposalRepository,
)

__all__ = [
    "SqlAlchemyCanonicalAnchorRepository",
    "SqlAlchemyProposalRepository",
    "VaultNoteRepository",
]
