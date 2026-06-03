"""Per-workspace Product CRUD endpoints (Workflow §3).

Five endpoints: list / create / get / patch / delete + Lift A v2's
``GET /{product_id}/bootstrap`` progress reader. Every read and every
mutation is scoped to the caller's workspace via :func:`get_workspace_id`;
a cross-workspace product is uniformly 404.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.api.deps import (
    get_db_session,
    get_db_session_factory,
    get_workspace_id,
    require_role,
)
from backend.identity.workspaces_db import ProductRow

from ._schemas import (
    ProductBootstrapResponse,
    ProductCreate,
    ProductResponse,
    ProductUpdate,
)

logger = structlog.get_logger(__name__)

router = APIRouter()


#: Callable shape the create handler uses to enqueue the post-commit
#: bootstrap job. Tests override via :func:`get_bootstrap_scheduler` so
#: the background task can be observed deterministically; production
#: wires :func:`schedule_product_bootstrap`.
BootstrapScheduler = Callable[
    ...,
    object,  # asyncio.Task[None] — typed loosely so overrides can return None
]


def get_bootstrap_scheduler() -> BootstrapScheduler:
    """Resolve the bootstrap scheduler.

    Production returns the real fire-and-forget scheduler; tests override
    this dep with a no-op (or a deterministic invoke-immediately closure).
    """
    from backend.workflow.application.runtime.product_bootstrap_runtime import (  # noqa: PLC0415
        schedule_product_bootstrap,
    )

    return schedule_product_bootstrap


@router.get("")
async def list_products(
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> list[ProductResponse]:
    rows = (
        (
            await session.execute(
                select(ProductRow)
                .where(ProductRow.workspace_id == workspace_id)
                .order_by(ProductRow.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return [ProductResponse.model_validate(r) for r in rows]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_product(
    payload: ProductCreate,
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_db_session_factory)],
    schedule_bootstrap: Annotated[BootstrapScheduler, Depends(get_bootstrap_scheduler)],
) -> ProductResponse:
    # Lift A v2 — stamp ``bootstrap_status="pending"`` synchronously on a
    # row carrying a repo_url so the response (and the founder UI) sees
    # the bootstrap is queued before the background task even starts.
    initial_bootstrap_status = "pending" if payload.repo_url else None
    row = ProductRow(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        name=payload.name,
        slug=payload.slug,
        repo_url=payload.repo_url,
        bootstrap_status=initial_bootstrap_status,
    )
    session.add(row)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"slug={payload.slug!r} already exists in this workspace",
        ) from None
    # W1 — every product gets a canonical git workspace immediately.
    # The init is FS-side; a failure here logs but doesn't undo the DB
    # commit (the product is real; the workspace can be re-initialised on
    # next request via the startup hook). The strict failure path (raise +
    # rollback) would force the founder to retry the whole POST on a
    # transient disk error, which is a worse UX.
    from backend.storage.product_workspace import (  # noqa: PLC0415 — lazy to avoid import cycle
        ProductWorkspaceError,
        init_product_workspace,
    )

    try:
        await init_product_workspace(row.id)
    except ProductWorkspaceError:
        logger.warning(
            "product_workspace_init_failed_at_create",
            product_id=str(row.id),
            exc_info=True,
        )
    # Lift A v2 — when the founder supplied a repo_url, hand off the
    # clone + ingest to the background scheduler. The response returns
    # 201 immediately with ``bootstrap_status="pending"`` and the UI
    # polls ``GET .../bootstrap`` for progress.
    if payload.repo_url:
        try:
            schedule_bootstrap(
                product_id=row.id,
                workspace_id=workspace_id,
                repo_url=payload.repo_url,
                session_factory=session_factory,
            )
        except Exception:  # noqa: BLE001
            # Scheduling itself failing (e.g. no running loop in a sync
            # test path) must NOT undo the row — the founder still owns
            # a product; they can retry the bootstrap via a re-create or
            # a future re-bootstrap action. Log + move on.
            logger.warning(
                "product_bootstrap_schedule_failed",
                product_id=str(row.id),
                exc_info=True,
            )
    return ProductResponse.model_validate(row)


@router.get("/{product_id}")
async def get_product(
    product_id: uuid.UUID,
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> ProductResponse:
    row = await session.get(ProductRow, product_id)
    if row is None or row.workspace_id != workspace_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Product {product_id} not found"
        )
    return ProductResponse.model_validate(row)


@router.get("/{product_id}/bootstrap")
async def get_product_bootstrap(
    product_id: uuid.UUID,
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> ProductBootstrapResponse:
    """Read the current bootstrap progress for a product.

    404 when the product itself doesn't exist in this workspace; a product
    without a ``bootstrap_status`` (created without ``repo_url``) returns
    200 with ``status=None`` so the founder UI can render "no bootstrap"
    without a special-case error path.
    """
    row = await session.get(ProductRow, product_id)
    if row is None or row.workspace_id != workspace_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Product {product_id} not found",
        )
    return ProductBootstrapResponse(
        product_id=row.id,
        status=row.bootstrap_status,
        artifacts_count=row.bootstrap_artifacts_count,
        error=row.bootstrap_error,
        run_id=row.bootstrap_run_id,
        # Surfaced from row timestamps — v1 surfaces a coarse "did the
        # job start / finish" only. A dedicated absolute-time pair is a
        # future lift if the founder ever asks.
        started_at=row.created_at if row.bootstrap_status else None,
        completed_at=(row.updated_at if row.bootstrap_status == "complete" else None),
    )


@router.patch("/{product_id}")
async def update_product(
    product_id: uuid.UUID,
    payload: ProductUpdate,
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> ProductResponse:
    row = await session.get(ProductRow, product_id)
    if row is None or row.workspace_id != workspace_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Product {product_id} not found"
        )
    for field in ("name", "repo_url"):
        value = getattr(payload, field)
        if value is not None:
            setattr(row, field, value)
    await session.commit()
    return ProductResponse.model_validate(row)


@router.delete(
    "/{product_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_role("admin"))],
)
async def delete_product(
    product_id: uuid.UUID,
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> None:
    row = await session.get(ProductRow, product_id)
    if row is None or row.workspace_id != workspace_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Product {product_id} not found"
        )
    await session.delete(row)
    await session.commit()


__all__ = ["BootstrapScheduler", "get_bootstrap_scheduler", "router"]
