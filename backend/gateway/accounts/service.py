"""ModelAccountService — orchestrates encryption + repository CRUD.

The service is the public API the rest of the gateway (and eventually
the REST router) calls; never mutate :class:`ModelAccount` rows or
encrypted blobs from elsewhere.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from backend.gateway.accounts.crypto import CredentialCipher
from backend.gateway.accounts.models import ModelAccount
from backend.gateway.accounts.repository import ModelAccountRepository
from backend.gateway.accounts.schemas import (
    ModelAccountCreate,
    ModelAccountOut,
    ModelAccountUpdate,
)

# Stable label used by the workspace-bootstrap path to seed a default
# personal account so single-user flows don't have to mint one manually.
DEFAULT_ACCOUNT_LABEL = "default"


class ModelAccountService:
    def __init__(self, session: AsyncSession, *, cipher: CredentialCipher) -> None:
        self._repo = ModelAccountRepository(session)
        self._cipher = cipher

    async def create(
        self,
        *,
        workspace_id: uuid.UUID,
        account_id: uuid.UUID,
        payload: ModelAccountCreate,
    ) -> ModelAccountOut:
        encrypted = self._cipher.encrypt(payload.api_key)
        row = await self._repo.create(
            workspace_id=workspace_id,
            account_id=account_id,
            provider=payload.provider,
            label=payload.label,
            litellm_model=payload.litellm_model,
            api_base=payload.api_base,
            api_key_encrypted=encrypted,
            data_jurisdiction=payload.data_jurisdiction,
            extra_params=dict(payload.extra_params),
        )
        return ModelAccountOut.from_model(row)

    async def list_(
        self,
        *,
        workspace_id: uuid.UUID,
        account_id: uuid.UUID,
        only_active: bool = False,
    ) -> list[ModelAccountOut]:
        rows = await self._repo.list_(
            workspace_id=workspace_id,
            account_id=account_id,
            only_active=only_active,
        )
        return [ModelAccountOut.from_model(r) for r in rows]

    async def get(
        self,
        *,
        workspace_id: uuid.UUID,
        account_id: uuid.UUID,
        model_account_id: uuid.UUID,
    ) -> ModelAccountOut | None:
        row = await self._repo.get(
            workspace_id=workspace_id,
            account_id=account_id,
            model_account_id=model_account_id,
        )
        return ModelAccountOut.from_model(row) if row is not None else None

    async def update(
        self,
        *,
        workspace_id: uuid.UUID,
        account_id: uuid.UUID,
        model_account_id: uuid.UUID,
        payload: ModelAccountUpdate,
    ) -> ModelAccountOut | None:
        row = await self._repo.get(
            workspace_id=workspace_id,
            account_id=account_id,
            model_account_id=model_account_id,
        )
        if row is None:
            return None
        fields = payload.model_dump(exclude_unset=True)
        if "api_key" in fields:
            row.api_key_encrypted = self._cipher.encrypt(fields.pop("api_key"))
        await self._repo.update(row, **fields)
        return ModelAccountOut.from_model(row)

    async def delete(
        self,
        *,
        workspace_id: uuid.UUID,
        account_id: uuid.UUID,
        model_account_id: uuid.UUID,
    ) -> bool:
        return await self._repo.delete(
            workspace_id=workspace_id,
            account_id=account_id,
            model_account_id=model_account_id,
        )

    def reveal_api_key(self, row: ModelAccount) -> str:
        """Decrypt — only the dispatch path should call this."""
        return self._cipher.decrypt(row.api_key_encrypted)
