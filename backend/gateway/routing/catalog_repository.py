"""SQL CRUD for ``model_catalog_entries``, account-scoped."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.gateway.routing.db import ModelCatalogEntryRow


class ModelCatalogDuplicateError(Exception):
    """Raised on ``(workspace_id, account_id, name)`` collision."""


class ModelCatalogRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        workspace_id: uuid.UUID,
        account_id: uuid.UUID,
        name: str,
        origin: str,
        litellm_model: str | None,
        litellm_params: dict[str, Any] | None,
        is_passthrough: bool,
    ) -> ModelCatalogEntryRow:
        row = ModelCatalogEntryRow(
            workspace_id=workspace_id,
            account_id=account_id,
            name=name,
            origin=origin,
            litellm_model=litellm_model,
            litellm_params=litellm_params,
            is_passthrough=is_passthrough,
        )
        self._session.add(row)
        try:
            await self._session.flush()
        except IntegrityError as exc:
            await self._session.rollback()
            raise ModelCatalogDuplicateError(str(exc.orig)) from exc
        return row

    async def get(
        self,
        entry_id: uuid.UUID,
        *,
        workspace_id: uuid.UUID,
        account_id: uuid.UUID,
    ) -> ModelCatalogEntryRow | None:
        stmt = select(ModelCatalogEntryRow).where(
            ModelCatalogEntryRow.id == entry_id,
            ModelCatalogEntryRow.workspace_id == workspace_id,
            ModelCatalogEntryRow.account_id == account_id,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_for_account(
        self,
        *,
        workspace_id: uuid.UUID,
        account_id: uuid.UUID,
    ) -> Sequence[ModelCatalogEntryRow]:
        stmt = (
            select(ModelCatalogEntryRow)
            .where(
                ModelCatalogEntryRow.workspace_id == workspace_id,
                ModelCatalogEntryRow.account_id == account_id,
            )
            .order_by(ModelCatalogEntryRow.name.asc())
        )
        return (await self._session.execute(stmt)).scalars().all()

    async def delete(
        self,
        entry_id: uuid.UUID,
        *,
        workspace_id: uuid.UUID,
        account_id: uuid.UUID,
    ) -> bool:
        row = await self.get(entry_id, workspace_id=workspace_id, account_id=account_id)
        if row is None:
            return False
        await self._session.delete(row)
        await self._session.flush()
        return True
