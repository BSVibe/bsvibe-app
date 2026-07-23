# bsvibe:stable-internal — modifications require a design doc update.
# Owners: workflow/domain/repositories
"""RunRepository Protocol — read/write seam for the ExecutionRun aggregate.

v8 D44/D45. Application-layer code (REST handlers, AgentRunner, workers)
calls this Protocol instead of issuing raw
``await session.execute(select(ExecutionRun)...)`` queries directly. The
SQLAlchemy implementation lives in
:mod:`backend.workflow.infrastructure.repositories.run_repository_sql`.

Method surface intentionally minimal — only the patterns existing callers
actually need today, so the Protocol stays honest. Add a method per real
caller, never speculatively.
"""

from __future__ import annotations

import uuid
from typing import Protocol, runtime_checkable

from backend.workflow.infrastructure.db import ExecutionRun


@runtime_checkable
class RunRepository(Protocol):
    """Persistence seam for :class:`ExecutionRun` rows."""

    async def get(self, run_id: uuid.UUID) -> ExecutionRun | None:
        """Return the run with this id, or ``None`` if it doesn't exist."""

    async def list_by_workspace(
        self, workspace_id: uuid.UUID, *, limit: int = 50
    ) -> list[ExecutionRun]:
        """Return recent runs in this workspace, newest-first (created_at desc)."""

    async def list_by_product(
        self, workspace_id: uuid.UUID, product_id: uuid.UUID, *, limit: int = 10
    ) -> list[ExecutionRun]:
        """Return recent runs for one product, newest-first (created_at desc).

        Workspace-scoped for tenancy + to engage the composite
        ``ix_execution_runs_ws_product`` index. Consumed by the product-tick
        planner to summarize what has already been attempted for the product.
        """

    async def find_by_request_id(self, request_id: uuid.UUID) -> ExecutionRun | None:
        """Return the (single) run wired to this Request, or ``None``.

        AgentRunner.open_run idempotency probe — when a Request already has a
        run, the second call must observe it and return the existing run id.
        """

    async def add(self, run: ExecutionRun) -> None:
        """Stage a new run for INSERT on the next flush.

        The repository does NOT flush or commit — transaction boundaries are
        owned at the application service / request scope (v8 D45).
        """


__all__ = ["RunRepository"]
