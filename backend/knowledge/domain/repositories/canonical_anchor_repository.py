"""CanonicalAnchorRepository Protocol — read/write seam for canonical anchors.

v8 D44/D45. The canonical-anchor index lives in PG (the
``canonical_anchors`` table) — per-workspace concept name → anchor row. The
GDPR compliance export reads this list; future canonicalization promotion
paths will add/update anchors through this same Protocol.

Concrete impl: :mod:`backend.knowledge.infrastructure.repositories.canonical_anchor_repository_sql`.
"""

from __future__ import annotations

import uuid
from typing import Protocol, runtime_checkable

from backend.knowledge.canonicalization.db import CanonicalAnchor


@runtime_checkable
class CanonicalAnchorRepository(Protocol):
    """Persistence seam for :class:`CanonicalAnchor` rows."""

    async def get(self, anchor_id: uuid.UUID) -> CanonicalAnchor | None:
        """Return the anchor with this id, or ``None`` if it doesn't exist."""

    async def find_by_name(self, workspace_id: uuid.UUID, name: str) -> CanonicalAnchor | None:
        """Return the workspace's anchor named ``name``, or ``None``.

        The ``(workspace_id, name)`` pair is UNIQUE so at most one row.
        """

    async def list_by_workspace(
        self, workspace_id: uuid.UUID, *, limit: int | None = None
    ) -> list[CanonicalAnchor]:
        """Return every anchor in this workspace (name asc).

        Used by the GDPR Art. 15 / 20 export — the limit defaults to ``None``
        so callers get the full set without paginating; pass an explicit
        limit when constructing a UI listing.
        """

    async def add(self, anchor: CanonicalAnchor) -> None:
        """Stage a new anchor for INSERT on the next flush.

        The repository does NOT flush or commit — transaction boundaries are
        owned at the application service / request scope (v8 D45).
        """


__all__ = ["CanonicalAnchorRepository"]
