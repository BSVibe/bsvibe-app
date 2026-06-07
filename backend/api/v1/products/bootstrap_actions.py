"""``/api/v1/products/{slug_or_id}/bootstrap/{cancel,retry}`` — Lift E13.

Recoverability for a wedged bootstrap. The qazasa123 dogfood found a
product whose ingest had been stuck "ingesting" for 6+ hours with no
founder-visible escape hatch. Two endpoints close the gap:

* ``cancel`` — flip an in-flight bootstrap to ``failed`` with a precise
  reason; opportunistically ``task.cancel()`` the in-process asyncio task
  if the runtime is hosting one.
* ``retry``  — reset the bootstrap lifecycle fields + re-schedule the
  same ``repo_url`` under the same product_id (no slug churn).

Both endpoints accept the slug-or-uuid the rest of the product surface
uses, and both REFUSE the wrong status with a precise HTTP error rather
than a silent no-op (cancel on a terminal status; retry on an in-flight
status). These mirror the MCP tools ``bsvibe_products_bootstrap_cancel``
/ ``bsvibe_products_bootstrap_retry`` field-for-field.
"""

from __future__ import annotations

import uuid
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.api.deps import get_db_session, get_db_session_factory, get_workspace_id
from backend.identity.workspaces_db import ProductRow

from ._schemas import ProductResponse
from .products_crud import BootstrapScheduler, get_bootstrap_scheduler

logger = structlog.get_logger(__name__)

router = APIRouter()


_IN_FLIGHT_STATUSES = frozenset({"pending", "cloning", "analyzing", "ingesting"})


async def _resolve_product(
    session: AsyncSession, workspace_id: uuid.UUID, slug_or_id: str
) -> ProductRow:
    """Resolve a product by slug-or-uuid, scoped to ``workspace_id``.

    Mirrors the MCP tool's resolver so the REST and MCP cancel/retry
    surfaces address the same row for the same input.
    """
    try:
        pid = uuid.UUID(slug_or_id)
    except ValueError:
        pid = None
    row: ProductRow | None = None
    if pid is not None:
        candidate = await session.get(ProductRow, pid)
        if candidate is not None and candidate.workspace_id == workspace_id:
            row = candidate
    if row is None:
        row = (
            await session.execute(
                select(ProductRow).where(
                    ProductRow.workspace_id == workspace_id,
                    ProductRow.slug == slug_or_id,
                )
            )
        ).scalar_one_or_none()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Product {slug_or_id} not found",
        )
    return row


@router.post("/{slug_or_id}/bootstrap/cancel")
async def cancel_product_bootstrap(
    slug_or_id: str,
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> ProductResponse:
    """Abort an in-flight bootstrap (Lift E13).

    ``status`` must be one of the in-flight values
    (``pending`` / ``cloning`` / ``analyzing`` / ``ingesting``); a
    terminal status (``complete`` / ``failed*``) is a 409 no-op.

    The row flip is the source of truth; the in-process task is
    opportunistically ``cancel()``'d if the runtime is hosting one — so
    the founder doesn't have to wait for the chunk loop to drain its
    current 1800s chunk.
    """
    row = await _resolve_product(session, workspace_id, slug_or_id)
    status_value = row.bootstrap_status
    if status_value not in _IN_FLIGHT_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"no-op — bootstrap is terminal (status={status_value!r})",
        )

    from backend.workflow.application.runtime.product_bootstrap_runtime import (  # noqa: PLC0415
        get_running_task,
    )

    task = get_running_task(row.id)
    if task is not None and not task.done():
        task.cancel()

    row.bootstrap_status = "failed"
    row.bootstrap_error = "cancelled by founder"
    await session.commit()
    await session.refresh(row)
    return ProductResponse.model_validate(row)


@router.post("/{slug_or_id}/bootstrap/retry")
async def retry_product_bootstrap(
    slug_or_id: str,
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_db_session_factory)],
    schedule_bootstrap: Annotated[BootstrapScheduler, Depends(get_bootstrap_scheduler)],
) -> ProductResponse:
    """Re-schedule bootstrap on an existing product row (Lift E13).

    No slug churn — the same product_id picks up the new ingest. Resets
    bootstrap_status / artifacts_count / error / progress to a clean
    pending state, then hands off to the same scheduler the create
    handler uses.

    409 when the bootstrap is already in flight (the founder must call
    cancel first); 400 when the product carries no ``repo_url``.
    """
    row = await _resolve_product(session, workspace_id, slug_or_id)
    if not row.repo_url:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="product has no repo_url to bootstrap from",
        )
    if row.bootstrap_status in _IN_FLIGHT_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="bootstrap already in flight — call bootstrap_cancel first",
        )

    row.bootstrap_status = "pending"
    row.bootstrap_artifacts_count = None
    row.bootstrap_error = None
    row.bootstrap_progress = None
    await session.commit()
    await session.refresh(row)

    try:
        schedule_bootstrap(
            product_id=row.id,
            workspace_id=workspace_id,
            repo_url=row.repo_url,
            session_factory=session_factory,
        )
    except Exception:  # noqa: BLE001 — soft-fail; row is already reset
        logger.warning(
            "product_bootstrap_retry_schedule_failed",
            product_id=str(row.id),
            exc_info=True,
        )

    return ProductResponse.model_validate(row)


__all__ = ["router"]
