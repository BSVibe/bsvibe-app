"""DecisionRepository Protocol — read/write seam for the Decision aggregate.

v8 D44/D45. The Decision aggregate is the founder's re-entry point for a
paused-run question (Workflow §5 #4). Application code — REST endpoints
(``/api/v1/checkpoints``), the run-detail handler, and the resolution
service — calls this Protocol instead of issuing raw ``select(Decision)``
queries.

Method surface limited to what existing callers actually use today.
"""

from __future__ import annotations

import uuid
from typing import Protocol, runtime_checkable

from backend.workflow.infrastructure.db import Decision


@runtime_checkable
class DecisionRepository(Protocol):
    """Persistence seam for :class:`Decision` rows."""

    async def get(self, decision_id: uuid.UUID) -> Decision | None:
        """Return the decision with this id, or ``None`` if it doesn't exist."""

    async def list_pending_by_workspace(self, workspace_id: uuid.UUID) -> list[Decision]:
        """PENDING decisions in this workspace, newest-first.

        Powers ``GET /api/v1/checkpoints`` — the founder's "things waiting
        for me to answer" inbox.
        """

    async def list_resolved_by_workspace(self, workspace_id: uuid.UUID) -> list[Decision]:
        """RESOLVED decisions in this workspace, most-recently-resolved first.

        Powers ``GET /api/v1/checkpoints/resolved`` — the Decisions "Resolved"
        tab. ``resolved_at`` desc, ``created_at`` desc as a stable tiebreak.
        """

    async def list_by_run(self, run_id: uuid.UUID, workspace_id: uuid.UUID) -> list[Decision]:
        """All decisions for one run (any status), newest-first.

        Powers ``GET /api/v1/runs/{id}/detail``. ``workspace_id`` is a
        defense-in-depth scope filter — even though the run is scoped on the
        caller side, the query stays workspace-pinned.
        """

    async def add(self, decision: Decision) -> None:
        """Stage a new decision for INSERT on the next flush.

        The repository does NOT flush or commit; the caller owns the
        transaction boundary (v8 D45).
        """


__all__ = ["DecisionRepository"]
