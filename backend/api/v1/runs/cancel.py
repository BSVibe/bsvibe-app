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
from backend.workflow.application.run_cleanup import cancel_run as cancel_run_service
from backend.workflow.infrastructure.db import RunStatus

from ._schemas import RunCancelResponse

router = APIRouter()


@router.post("/{run_id}/cancel")
async def cancel_run(
    run_id: uuid.UUID,
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> RunCancelResponse:
    """Cancel an OPEN / RUNNING run. 404 cross-workspace / unknown; 409 terminal.

    Delegates to the ``cancel_run`` application service (the same one the MCP
    ``bsvibe_runs_cancel`` tool uses) so BOTH paths resolve the run's PENDING
    decisions + abort a mid-verify merge — cancelling via the transition alone
    left the Summary "확인 필요" card up forever (orphaned-half).
    """
    outcome = await cancel_run_service(
        session,
        run_id=run_id,
        workspace_id=workspace_id,
        reason="founder cancelled",
    )
    if not outcome.found:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Run {run_id} not found")
    if not outcome.cancelled:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Run {run_id} is {outcome.status}; only an in-flight run can be cancelled",
        )
    await session.commit()

    return RunCancelResponse(id=run_id, status=RunStatus.CANCELLED)


__all__ = ["router"]
