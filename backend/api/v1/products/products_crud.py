"""Per-workspace Product CRUD endpoints (Workflow §3).

Five endpoints: list / create / get / patch / delete. Every read and every
mutation is scoped to the caller's workspace via :func:`get_workspace_id`; a
cross-workspace product is uniformly 404.
"""

from __future__ import annotations

import uuid
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import get_db_session, get_workspace_id, require_role
from backend.identity.workspaces_db import ProductRow

from ._schemas import ProductCreate, ProductResponse, ProductUpdate

logger = structlog.get_logger(__name__)

router = APIRouter()


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
) -> ProductResponse:
    row = ProductRow(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        name=payload.name,
        slug=payload.slug,
        repo_url=payload.repo_url,
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


__all__ = ["router"]
