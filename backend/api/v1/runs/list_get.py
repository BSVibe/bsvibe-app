"""Read endpoints for ``/api/v1/runs`` — list + single-row fetch.

Strictly read-only adapters (D35 thin-adapter): parse the workspace + repo
out of DI, hand off to :class:`RunRepository`, serialize the rows. Mutations
are intentionally absent here — runs are created by the agent loop / workers
(Bundle G), never by an HTTP POST. The richer per-run inspection surface
lives in :mod:`.detail`.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import get_db_session, get_workspace_id
from backend.api.v1._workflow_deps import get_run_repository
from backend.workflow.application.run_detail import (
    NO_VERIFICATION_ROUNDS,
    VerificationRounds,
    _frame_field,
    _intent_of,
    _restarted_at_of,
    first_round_pass_rate,
    verification_quality,
    verification_rounds_by_run,
)
from backend.workflow.domain.repositories import RunRepository
from backend.workflow.infrastructure.db import ExecutionRun
from backend.workflow.serialization.run_views import RunResponse

router = APIRouter()


def _run_response(row: ExecutionRun, rounds: VerificationRounds) -> RunResponse:
    return RunResponse(
        id=row.id,
        workspace_id=row.workspace_id,
        product_id=row.product_id,
        request_id=row.request_id,
        status=row.status,
        intent=_intent_of(row.payload),
        summary_title=_frame_field(row.payload, "summary_title"),
        framed_intent=_frame_field(row.payload, "framed_intent"),
        restarted_at=_restarted_at_of(row.payload),
        verification_rounds=rounds.rounds,
        passed_on_round=rounds.passed_on_round,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@router.get("")
async def list_runs(
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    runs: Annotated[RunRepository, Depends(get_run_repository)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    limit: int = 50,
    product_id: uuid.UUID | None = None,
) -> list[RunResponse]:
    """List recent ExecutionRun rows for the workspace, newest first.

    ``product_id`` narrows to one product. The narrowing happens in the query,
    not after the page is cut, so the answer for a quiet product does not
    depend on how busy the rest of the workspace has been.
    """
    limit = max(1, min(limit, 200))
    rows = (
        await runs.list_by_product(workspace_id, product_id, limit=limit)
        if product_id is not None
        else await runs.list_by_workspace(workspace_id, limit=limit)
    )
    # One query for every row's rounds — never one per run.
    rounds = await verification_rounds_by_run(session, workspace_id, [r.id for r in rows])
    return [_run_response(row, rounds.get(row.id, NO_VERIFICATION_ROUNDS)) for row in rows]


@router.get("/first-round-pass-rate")
async def get_first_round_pass_rate(
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    limit: Annotated[int, Query(ge=1, le=200)] = 30,
    product_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """Share of the latest finished runs that passed verification on round 1.

    Same function as the ``bsvibe_runs_first_round_pass_rate`` MCP tool.
    ``product_id`` narrows the runs counted to one product.
    """
    rate = await first_round_pass_rate(session, workspace_id, limit=limit, product_id=product_id)
    return rate.as_dict()


@router.get("/verification-quality")
async def get_verification_quality(
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    limit: Annotated[int, Query(ge=1, le=200)] = 30,
    product_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """Verification-quality figures over the latest finished runs.

    Same function as the ``bsvibe_runs_verification_quality`` MCP tool.
    """
    quality = await verification_quality(session, workspace_id, limit=limit, product_id=product_id)
    return quality.as_dict()


@router.get("/{run_id}")
async def get_run(
    run_id: uuid.UUID,
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    runs: Annotated[RunRepository, Depends(get_run_repository)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> RunResponse:
    """Fetch one ExecutionRun by id, scoped to the caller's workspace."""
    row = await runs.get(run_id)
    if row is None or row.workspace_id != workspace_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Run {run_id} not found")
    rounds = await verification_rounds_by_run(session, workspace_id, [row.id])
    return _run_response(row, rounds.get(row.id, NO_VERIFICATION_ROUNDS))


__all__ = ["router"]
