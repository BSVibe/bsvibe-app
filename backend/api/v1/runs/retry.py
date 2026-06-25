"""``POST /api/v1/runs/{run_id}/retry`` — re-open a terminal-failed run.

L2 (#9). Runs are still never *created* via HTTP (the agent loop / workers own
creation — see the package docstring). Retry is the one founder-initiated
mutation on an existing run: a run that ended ``FAILED`` or ``CANCELLED`` is
transitioned back to ``OPEN`` so ``AgentWorker.drive_once`` re-picks it and
drives a fresh attempt. A failed run is recoverable, not a dead-end.

* A non-terminal run (``open`` / ``running`` / ``review_ready`` / ``shipped``)
  → 409 (there is nothing to retry).
* A cross-workspace / unknown id → 404 (never a leak).
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import get_db_session, get_workspace_id
from backend.api.v1._workflow_deps import get_run_repository
from backend.workflow.application.agent_runner import AgentRunner
from backend.workflow.domain.repositories import RunRepository
from backend.workflow.infrastructure.db import RunStatus

from ._schemas import RunRetryResponse

router = APIRouter()

# Only a terminal-FAILED run can be retried. A paused (RUNNING, needs-decision)
# run is resolved via the Decision's ``retry`` action instead.
_RETRYABLE = frozenset({RunStatus.FAILED, RunStatus.CANCELLED})


@router.post("/{run_id}/retry")
async def retry_run(
    run_id: uuid.UUID,
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    runs: Annotated[RunRepository, Depends(get_run_repository)],
) -> RunRetryResponse:
    """Re-open a FAILED / CANCELLED run for another attempt.

    404 when the run is not in the caller's workspace or is unknown; 409 when
    the run is not in a terminal-failed state.
    """
    run = await runs.get(run_id)
    if run is None or run.workspace_id != workspace_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Run {run_id} not found")
    if run.status not in _RETRYABLE:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Run {run_id} is {run.status.value}; only a failed or cancelled run can be retried"
            ),
        )

    # Bump a retry marker on the free-form payload (re-assign, not in-place
    # mutate, so SQLAlchemy detects the JSON-column change). drive_once preserves
    # it (it spreads the existing payload), so observability + future loop logic
    # can see this is a re-attempt.
    payload: dict[str, Any] = dict(run.payload or {})
    retry_count = int(payload.get("retry_count", 0)) + 1
    payload["retry_count"] = retry_count
    run.payload = payload
    await session.flush()

    # FAILED / CANCELLED → OPEN so AgentWorker.drive_once (scans OPEN runs)
    # re-picks it for a fresh attempt. The history row records the re-open.
    runner = AgentRunner(session)
    await runner.transition(
        run_id=run.id,
        to_status=RunStatus.OPEN,
        reason=f"founder retry (attempt {retry_count + 1})",
    )
    await session.commit()

    return RunRetryResponse(id=run.id, status=run.status, retry_count=retry_count)


__all__ = ["router"]
