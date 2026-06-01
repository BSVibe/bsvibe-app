"""DeliverableRepository Protocol — read/write seam for the Deliverable aggregate.

v8 D44/D45. The Deliverable aggregate is the produced artifact tied to a run
(Workflow §3 — PR, page, direct output, etc.). Application code — REST
endpoints (``/api/v1/deliverables``), the delivery dispatcher, the
DeliveryWorker, the agent_runner spec-handoff path, and the checkpoints
``_ship_decision_run`` path — calls this Protocol instead of issuing raw
``select(Deliverable)`` / ``session.get(Deliverable, ...)`` queries.

Method surface limited to what existing callers actually use today. New
methods get added per real caller, never speculatively.
"""

from __future__ import annotations

import uuid
from typing import Protocol, runtime_checkable

from backend.workflow.infrastructure.db import Deliverable


@runtime_checkable
class DeliverableRepository(Protocol):
    """Persistence seam for :class:`Deliverable` rows."""

    async def get(self, deliverable_id: uuid.UUID) -> Deliverable | None:
        """Return the deliverable with this id, or ``None`` if it doesn't exist."""

    async def list_by_workspace(
        self,
        workspace_id: uuid.UUID,
        *,
        run_id: uuid.UUID | None = None,
        limit: int = 50,
    ) -> list[Deliverable]:
        """Recent deliverables for this workspace, newest-first.

        Optional ``run_id`` narrows to one run's deliverables. ``limit`` is
        bounded by the caller (REST handler clamps to 200). Powers
        ``GET /api/v1/deliverables``.
        """

    async def list_by_run(self, run_id: uuid.UUID, workspace_id: uuid.UUID) -> list[Deliverable]:
        """Every deliverable for one run (oldest-first by ``created_at``).

        Powers the run-detail handler (``/api/v1/runs/{id}/detail``); the
        oldest-first ordering matches the streaming order the loop emitted
        the partial artifacts in. ``workspace_id`` is a defense-in-depth
        scope filter — the run is already workspace-scoped on the caller
        side, but the query stays pinned.
        """

    async def list_by_run_id(self, run_id: uuid.UUID) -> list[Deliverable]:
        """Deliverables for one run, no workspace filter (system-level read).

        Used by the agent_runner spec-handoff path where the calling code has
        the design run id but not yet rehydrated the workspace context.
        """

    async def find_first_by_run(self, run_id: uuid.UUID) -> Deliverable | None:
        """First (any) deliverable for a run, or ``None``.

        Powers checkpoints ``_ship_decision_run`` short-circuit — verifier's
        PASS path already minted a Deliverable; don't mint a duplicate.
        """

    async def add(self, deliverable: Deliverable) -> None:
        """Stage a new deliverable for INSERT on the next flush.

        The repository does NOT flush or commit; the caller owns the
        transaction boundary (v8 D45).
        """


__all__ = ["DeliverableRepository"]
