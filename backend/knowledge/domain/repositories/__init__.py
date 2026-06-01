"""Repository Protocols — application-layer seam onto Knowledge persistence.

v8 §22 #11 + D44/D45. The Knowledge application layer depends on the
Protocols here, not on SQLAlchemy / filesystem primitives directly. Concrete
implementations live in :mod:`backend.knowledge.infrastructure.repositories`.

The first Knowledge Repository extraction (Lift I-Repo-Knowledge) ships:

* :class:`NoteRepository` — vault-backed CRUD over garden notes (the
  Knowledge SoT lives on the filesystem; the Protocol abstracts away
  ``StorageBackend`` / ``GardenWriter`` mechanics so application code reads
  + writes notes through a stable seam).
* :class:`ProposalRepository` — SQL-backed CRUD over
  :class:`~backend.knowledge.canonicalization.db.CanonicalizationProposal`
  rows (the canonicalization queue).
* :class:`CanonicalAnchorRepository` — SQL-backed read seam over
  :class:`~backend.knowledge.canonicalization.db.CanonicalAnchor` rows (the
  per-workspace concept index — used by compliance export today).

Deferred to follow-up sub-lifts (I-Repo-Knowledge-2):

* :class:`RegionRepository` — region routing rows.
* :class:`OntologyRepository` — subset ontology structure.
* :class:`CanonicalizationDecisionRepository` (kept distinct from Workflow's
  :class:`~backend.workflow.domain.repositories.DecisionRepository`).

Pragmatic choice: SQL repositories return the existing ORM row types rather
than separate plain-Python entities (matches the Lift I-Repo-Workflow
decision so the seam stays cheap to wire). The architectural seam —
application code depending on a Protocol, not on ``sqlalchemy.select`` or
``StorageBackend`` — is what reduces the v8 §22 #11 violation count.
"""

from __future__ import annotations

from backend.knowledge.domain.repositories.canonical_anchor_repository import (
    CanonicalAnchorRepository,
)
from backend.knowledge.domain.repositories.note_repository import (
    NoteRecord,
    NoteRepository,
)
from backend.knowledge.domain.repositories.proposal_repository import ProposalRepository

__all__ = [
    "CanonicalAnchorRepository",
    "NoteRecord",
    "NoteRepository",
    "ProposalRepository",
]
