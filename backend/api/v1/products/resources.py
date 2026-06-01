"""Product resources — named pointers a product works with (repo/doc/deploy/note).

Workspace-scoped exactly like the parent product: every route first resolves
the product within the caller's workspace and 404s otherwise, so a resource is
never reachable across a workspace boundary.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import get_db_session, get_workspace_id
from backend.identity.workspaces_db import ProductResourceRow

from ._helpers import _resolve_product_in_workspace
from ._schemas import ResourceCreate, ResourceResponse

router = APIRouter()


@router.get("/{product_id}/resources")
async def list_product_resources(
    product_id: uuid.UUID,
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> list[ResourceResponse]:
    await _resolve_product_in_workspace(session, product_id, workspace_id)
    rows = (
        (
            await session.execute(
                select(ProductResourceRow)
                .where(ProductResourceRow.product_id == product_id)
                .order_by(ProductResourceRow.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return [ResourceResponse.model_validate(r) for r in rows]


@router.post("/{product_id}/resources", status_code=status.HTTP_201_CREATED)
async def add_product_resource(
    product_id: uuid.UUID,
    payload: ResourceCreate,
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> ResourceResponse:
    await _resolve_product_in_workspace(session, product_id, workspace_id)
    row = ProductResourceRow(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        product_id=product_id,
        kind=payload.kind,
        title=payload.title,
        url=payload.url,
        note=payload.note,
    )
    session.add(row)
    await session.commit()
    return ResourceResponse.model_validate(row)


@router.delete(
    "/{product_id}/resources/{resource_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_product_resource(
    product_id: uuid.UUID,
    resource_id: uuid.UUID,
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> None:
    await _resolve_product_in_workspace(session, product_id, workspace_id)
    row = await session.get(ProductResourceRow, resource_id)
    if row is None or row.product_id != product_id or row.workspace_id != workspace_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Resource {resource_id} not found"
        )
    await session.delete(row)
    await session.commit()


__all__ = ["router"]
