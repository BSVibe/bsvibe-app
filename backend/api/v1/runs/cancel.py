"""``POST /api/v1/runs/{run_id}/cancel`` — stop an in-flight run.

L9. The founder can STOP a run that is still OPEN (queued) or RUNNING
(in-flight): it transitions to CANCELLED. Cancel is cooperative — the
``AgentRunner.transition`` guard makes the worker's in-flight drive no-op its
post-drive transition, so a run cancelled mid-drive stays cancelled (the
orphaned compute finishes and is discarded). A cancelled run is recoverable via
``POST /runs/{run_id}/retry`` (CANCELLED → OPEN).

* A terminal run (``shipped`` / ``failed`` / ``cancelled``) → 409 (nothing to
  cancel).
* A cross-workspace / unknown id → 404.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import get_db_session, get_workspace_id
from backend.api.v1._workflow_deps import get_run_repository
from backend.workflow.application.agent_runner import AgentRunner
from backend.workflow.domain.repositories import RunRepository
from backend.workflow.infrastructure.db import RunStatus

from ._schemas import RunCancelResponse

router = APIRouter()

# Only an in-flight run can be cancelled; a terminal run has nothing to stop.
_CANCELLABLE = frozenset({RunStatus.OPEN, RunStatus.RUNNING})


@router.post("/{run_id}/cancel")
async def cancel_run(
    run_id: uuid.UUID,
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    runs: Annotated[RunRepository, Depends(get_run_repository)],
) -> RunCancelResponse:
    """Cancel an OPEN / RUNNING run. 404 cross-workspace / unknown; 409 terminal."""
    run = await runs.get(run_id)
    if run is None or run.workspace_id != workspace_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Run {run_id} not found")
    if run.status not in _CANCELLABLE:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Run {run_id} is {run.status.value}; only an in-flight run can be cancelled",
        )

    runner = AgentRunner(session)
    await runner.transition(
        run_id=run.id,
        to_status=RunStatus.CANCELLED,
        reason="founder cancelled",
    )
    await session.commit()

    return RunCancelResponse(id=run.id, status=run.status)


__all__ = ["router"]
