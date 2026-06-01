"""Resource bindings — per-Product × ConnectorAccount 3-knob binding (Workflow §3).

Carries ``selection`` / ``trigger`` / ``output_mode``. The Receive stage
(B10b) resolves an inbound webhook → binding → Product via
:meth:`ResourceBindingRepository.find_binding`; this surface is the founder's
CRUD to manage those bindings.

Workspace-scoped exactly like a product resource: every route first resolves
the product within the caller's workspace and 404s otherwise, and the binding
repository itself filters every read/write on ``workspace_id``.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import get_db_session, get_workspace_id
from backend.identity.infrastructure.repositories import (
    SqlAlchemyResourceBindingRepository,
)

from ._helpers import (
    _resolve_connector_account_in_workspace,
    _resolve_product_in_workspace,
)
from ._schemas import (
    ResourceBindingCreate,
    ResourceBindingResponse,
    ResourceBindingUpdate,
)

router = APIRouter()


@router.get("/{product_id}/bindings")
async def list_product_bindings(
    product_id: uuid.UUID,
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> list[ResourceBindingResponse]:
    await _resolve_product_in_workspace(session, product_id, workspace_id)
    repo = SqlAlchemyResourceBindingRepository(session)
    rows = await repo.list_for_product(workspace_id=workspace_id, product_id=product_id)
    return [ResourceBindingResponse.model_validate(r) for r in rows]


@router.post("/{product_id}/bindings", status_code=status.HTTP_201_CREATED)
async def create_product_binding(
    product_id: uuid.UUID,
    payload: ResourceBindingCreate,
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> ResourceBindingResponse:
    await _resolve_product_in_workspace(session, product_id, workspace_id)
    await _resolve_connector_account_in_workspace(
        session, payload.connector_account_id, workspace_id
    )
    repo = SqlAlchemyResourceBindingRepository(session)
    row = await repo.create(
        workspace_id=workspace_id,
        product_id=product_id,
        connector_account_id=payload.connector_account_id,
        resource_id=payload.resource_id,
        selection=payload.selection,
        trigger=payload.trigger.model_dump(),
        output_mode=payload.output_mode,
    )
    await session.commit()
    await session.refresh(row)
    return ResourceBindingResponse.model_validate(row)


@router.patch("/{product_id}/bindings/{binding_id}")
async def update_product_binding(
    product_id: uuid.UUID,
    binding_id: uuid.UUID,
    payload: ResourceBindingUpdate,
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> ResourceBindingResponse:
    await _resolve_product_in_workspace(session, product_id, workspace_id)
    repo = SqlAlchemyResourceBindingRepository(session)
    row = await repo.get(workspace_id=workspace_id, binding_id=binding_id)
    if row is None or row.product_id != product_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Binding {binding_id} not found",
        )
    await repo.update(
        row,
        selection=payload.selection,
        trigger=payload.trigger.model_dump() if payload.trigger is not None else None,
        output_mode=payload.output_mode,
    )
    await session.commit()
    await session.refresh(row)
    return ResourceBindingResponse.model_validate(row)


@router.delete(
    "/{product_id}/bindings/{binding_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_product_binding(
    product_id: uuid.UUID,
    binding_id: uuid.UUID,
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> None:
    await _resolve_product_in_workspace(session, product_id, workspace_id)
    # Scope check: the binding must belong to this product (and the repo's get
    # already enforces workspace scope).
    repo = SqlAlchemyResourceBindingRepository(session)
    row = await repo.get(workspace_id=workspace_id, binding_id=binding_id)
    if row is None or row.product_id != product_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Binding {binding_id} not found",
        )
    deleted = await repo.delete(workspace_id=workspace_id, binding_id=binding_id)
    if not deleted:
        # Concurrent delete — surface 404 uniformly.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Binding {binding_id} not found",
        )
    await session.commit()


__all__ = ["router"]
