"""/api/v1/accounts — ModelAccount CRUD for the workspace + billing account."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import get_db_session, get_workspace_id, require_account_id
from backend.router.accounts.crypto import CredentialCipher, _key_from_settings
from backend.router.accounts.schemas import (
    ModelAccountCreate,
    ModelAccountOut,
    ModelAccountUpdate,
)
from backend.router.accounts.service import ModelAccountService

router = APIRouter()


def _service(session: AsyncSession) -> ModelAccountService:
    return ModelAccountService(session, cipher=CredentialCipher(_key_from_settings()))


@router.get("")
async def list_accounts(
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    account_id: Annotated[uuid.UUID, Depends(require_account_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    only_active: bool = False,
) -> list[ModelAccountOut]:
    return await _service(session).list_(
        workspace_id=workspace_id,
        account_id=account_id,
        only_active=only_active,
    )


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_account(
    payload: ModelAccountCreate,
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    account_id: Annotated[uuid.UUID, Depends(require_account_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> ModelAccountOut:
    created = await _service(session).create(
        workspace_id=workspace_id, account_id=account_id, payload=payload
    )
    await session.commit()
    return created


@router.get("/{model_account_id}")
async def get_account(
    model_account_id: uuid.UUID,
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    account_id: Annotated[uuid.UUID, Depends(require_account_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> ModelAccountOut:
    result = await _service(session).get(
        workspace_id=workspace_id,
        account_id=account_id,
        model_account_id=model_account_id,
    )
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"ModelAccount {model_account_id} not found",
        )
    return result


@router.patch("/{model_account_id}")
async def update_account(
    model_account_id: uuid.UUID,
    payload: ModelAccountUpdate,
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    account_id: Annotated[uuid.UUID, Depends(require_account_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> ModelAccountOut:
    result = await _service(session).update(
        workspace_id=workspace_id,
        account_id=account_id,
        model_account_id=model_account_id,
        payload=payload,
    )
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"ModelAccount {model_account_id} not found",
        )
    await session.commit()
    return result


@router.delete("/{model_account_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_account(
    model_account_id: uuid.UUID,
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    account_id: Annotated[uuid.UUID, Depends(require_account_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> None:
    deleted = await _service(session).delete(
        workspace_id=workspace_id,
        account_id=account_id,
        model_account_id=model_account_id,
    )
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"ModelAccount {model_account_id} not found",
        )
    await session.commit()
