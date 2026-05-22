"""AgentRunner — drive the execution layer for one Request.

Workflow §12.5 #8 (Bundle G — Orchestrator). The agent runner is the
bridge between the workflow state machine and the execution layer
(Bundle X). It opens an ExecutionRun row, advances it through the
``open → running → review_ready → shipped`` lifecycle, and surfaces
the resulting run_id.

Phase 1 implementation: persists the run skeleton + transitions through
the lifecycle stages. The actual LLM tool loop / verification execution
is the next layer down (``backend.execution.orchestrator.RunOrchestrator``);
this module owns the *transactional* lifecycle, that module owns the
*compute* lifecycle.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from backend.execution.db import ExecutionRun, ExecutionRunHistory, RunStatus
from backend.intake.db import RequestRow

logger = structlog.get_logger(__name__)


class AgentRunner:
    """Spawn + supervise one ExecutionRun for a Request."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def open_run(self, *, request: RequestRow) -> uuid.UUID:
        """Mint an ExecutionRun row tied to ``request``; returns run_id.

        Idempotent: if a run for this request already exists, returns its
        id without creating a duplicate.
        """
        existing = await self._find_existing_run(request_id=request.id)
        if existing is not None:
            return existing.id

        run = ExecutionRun(
            id=uuid.uuid4(),
            workspace_id=request.workspace_id,
            product_id=None,
            request_id=request.id,
            status=RunStatus.OPEN,
            payload={"request_id": str(request.id)},
            created_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC),
        )
        self._session.add(run)
        self._session.add(
            ExecutionRunHistory(
                id=uuid.uuid4(),
                run_id=run.id,
                workspace_id=request.workspace_id,
                from_status=None,
                to_status=RunStatus.OPEN,
                reason="opened by agent_runner",
                created_at=datetime.now(tz=UTC),
            )
        )
        await self._session.flush()
        logger.info(
            "agent_runner_opened",
            request_id=str(request.id),
            run_id=str(run.id),
        )
        return run.id

    async def transition(
        self,
        *,
        run_id: uuid.UUID,
        to_status: RunStatus,
        reason: str | None = None,
    ) -> bool:
        """Append history + flip ExecutionRun.status. Returns False on no-op."""
        run = await self._session.get(ExecutionRun, run_id)
        if run is None:
            return False
        if run.status is to_status:
            return False
        from_status = run.status
        run.status = to_status
        run.updated_at = datetime.now(tz=UTC)
        self._session.add(
            ExecutionRunHistory(
                id=uuid.uuid4(),
                run_id=run_id,
                workspace_id=run.workspace_id,
                from_status=from_status,
                to_status=to_status,
                reason=reason,
                created_at=datetime.now(tz=UTC),
            )
        )
        await self._session.flush()
        logger.info(
            "agent_runner_transitioned",
            run_id=str(run_id),
            from_status=from_status.value,
            to_status=to_status.value,
        )
        return True

    async def _find_existing_run(self, *, request_id: uuid.UUID) -> ExecutionRun | None:
        from sqlalchemy import select  # noqa: PLC0415 — local-only lookup

        stmt = select(ExecutionRun).where(ExecutionRun.request_id == request_id).limit(1)
        return (await self._session.execute(stmt)).scalar_one_or_none()


__all__ = ["AgentRunner"]
