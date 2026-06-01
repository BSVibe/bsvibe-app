"""RequestRepository Protocol — read/write seam for the Request aggregate.

v8 D44/D45. The :class:`Request` (workflow §1's unit of work) lives in
:class:`backend.workflow.infrastructure.intake.db.RequestRow`. Application
code — the IntakeWorker (mints), the AgentWorker (claims), the
workspace-compliance export, and the REST surface — calls this Protocol
instead of issuing raw ``select(RequestRow)`` / ``session.get(RequestRow,
...)`` queries.

Method surface limited to what existing callers actually use today. New
methods get added per real caller, never speculatively.
"""

from __future__ import annotations

import uuid
from typing import Protocol, runtime_checkable

from backend.workflow.infrastructure.intake.db import RequestRow


@runtime_checkable
class RequestRepository(Protocol):
    """Persistence seam for :class:`RequestRow` rows."""

    async def get(self, request_id: uuid.UUID) -> RequestRow | None:
        """Return the request with this id, or ``None`` if it doesn't exist."""

    async def list_by_workspace(self, workspace_id: uuid.UUID) -> list[RequestRow]:
        """Every request in this workspace.

        Powers the workspace-compliance export (``GET
        /api/v1/workspaces/{id}/compliance-export``) — no ordering /
        pagination requirements today, the caller iterates the full set.
        """

    async def list_open_for_claim(self, *, limit: int = 50) -> list[RequestRow]:
        """Up to ``limit`` ``OPEN`` requests across ALL workspaces, oldest-first.

        Powers :class:`backend.workflow.infrastructure.workers.agent_worker.AgentWorker._claim_batch`
        — the AgentWorker drives whichever OPEN Request is at the head of the
        queue. The infrastructure caller composes ``.with_for_update(skip_locked=True)``
        on the returned select() when it needs the row lock; this Protocol
        surface returns the loaded rows, not a SELECT statement (worker-local
        locking concern stays inside the concrete impl).
        """

    async def add(self, request: RequestRow) -> None:
        """Stage a new request for INSERT on the next flush.

        The repository does NOT flush or commit; the caller owns the
        transaction boundary (v8 D45).
        """


__all__ = ["RequestRepository"]
