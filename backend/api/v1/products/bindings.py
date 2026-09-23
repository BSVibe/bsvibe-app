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
from collections.abc import Sequence
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import get_db_session, get_workspace_id
from backend.connectors.db import ConnectorAccountRow
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


async def _with_connector(
    session: AsyncSession, rows: Sequence[Any]
) -> list[ResourceBindingResponse]:
    """행들을 응답으로 바꾸면서 커넥터 종류를 채운다.

    계정 id → connector 를 **한 번에** 조회한다. relationship 지연 로딩을 쓰면
    async 세션에서 MissingGreenlet 이 나고, 행마다 조회하면 N+1 이다.
    """
    out: list[ResourceBindingResponse] = []
    account_ids = {r.connector_account_id for r in rows}
    kinds: dict[Any, str] = {}
    if account_ids:
        result = await session.execute(
            select(ConnectorAccountRow.id, ConnectorAccountRow.connector).where(
                ConnectorAccountRow.id.in_(account_ids)
            )
        )
        kinds = {row[0]: row[1] for row in result.all()}
    for r in rows:
        model = ResourceBindingResponse.model_validate(r)
        model.connector = kinds.get(r.connector_account_id)
        out.append(model)
    return out


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
    return await _with_connector(session, rows)


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
    return (await _with_connector(session, [row]))[0]


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
    return (await _with_connector(session, [row]))[0]


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
