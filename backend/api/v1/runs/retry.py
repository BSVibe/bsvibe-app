"""``POST /api/v1/runs/{run_id}/retry`` — re-open a terminal-failed run.

L2 (#9). Runs are still never *created* via HTTP (the agent loop / workers own
creation — see the package docstring). Retry is the one founder-initiated
mutation on an existing run: a run that ended ``FAILED`` or ``CANCELLED`` is
transitioned back to ``OPEN`` so ``AgentWorker.drive_once`` re-picks it and
drives a fresh attempt. A failed run is recoverable, not a dead-end.

This module is the thin HTTP adapter — the rule lives in
:func:`backend.workflow.application.run_cleanup.retry_run`, the same function
``bsvibe_runs_retry`` calls. The two surfaces share the rule and keep their own
error vocabulary (HTTP status codes here, ``ToolError`` there):

* A non-terminal run (``open`` / ``running`` / ``review_ready`` / ``shipped``)
  → 409 (there is nothing to retry).
* A cross-workspace / unknown id → 404 (never a leak).
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import get_db_session, get_workspace_id
from backend.workflow.application.run_cleanup import retry_run as retry_run_service
from backend.workflow.infrastructure.db import RunStatus
from backend.workflow.serialization.run_views import RunRetryResponse

router = APIRouter()


@router.post("/{run_id}/retry")
async def retry_run(
    run_id: uuid.UUID,
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> RunRetryResponse:
    """Re-open a FAILED / CANCELLED run for another attempt.

    Delegates to the ``retry_run`` application service (the same one the MCP
    ``bsvibe_runs_retry`` tool calls) and maps its outcome onto HTTP: 404 when
    the run is not in the caller's workspace or is unknown; 409 when the run is
    not in a terminal-failed state.
    """
    outcome = await retry_run_service(session, run_id=run_id, workspace_id=workspace_id)
    if not outcome.found:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Run {run_id} not found")
    if not outcome.retried:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Run {run_id} is {outcome.status}; only a failed or cancelled run can be retried"
            ),
        )
    await session.commit()

    # ``retried`` is only True on the FAILED/CANCELLED → OPEN flip, so the
    # outcome's status string is always OPEN here; naming the enum keeps the
    # response typed without re-parsing it.
    return RunRetryResponse(id=run_id, status=RunStatus.OPEN, retry_count=outcome.retry_count)


__all__ = ["router"]
