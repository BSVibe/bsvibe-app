"""ModelAccountService — orchestrates encryption + repository CRUD.

The service is the public API the rest of the gateway (and eventually
the REST router) calls; never mutate :class:`ModelAccount` rows or
encrypted blobs from elsewhere.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from backend.router.accounts.crypto import CredentialCipher
from backend.router.accounts.models import ModelAccount
from backend.router.accounts.repository import ModelAccountRepository
from backend.router.accounts.schemas import (
    ModelAccountCreate,
    ModelAccountOut,
    ModelAccountUpdate,
)

# Stable label used by the workspace-bootstrap path to seed a default
# personal account so single-user flows don't have to mint one manually.
DEFAULT_ACCOUNT_LABEL = "default"

# Providers whose models run on the operator's host — they don't authenticate
# with a real key, so a NULL ``api_key_encrypted`` is allowed and resolves to
# the empty string (litellm forwards it harmlessly). Every other provider
# requires a populated key; NULL there is a bug, not a no-op. This is a
# pure NULL-key tolerance list — NOT a tier verdict (Lift E2 removed the
# tier vocabulary; the provider field is only a label).
_NULL_KEY_TOLERANT_PROVIDERS = frozenset({"ollama", "lmstudio", "llama_cpp", "vllm"})


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
        """Decrypt — only the dispatch path should call this.

        Host-local providers (Ollama / LM Studio / llama.cpp / vLLM) carry
        no api key (the LLM runs on the operator's host); accept NULL and
        return an empty credential string. Every other provider: NULL is
        a bug, raise rather than silently dispatch with an empty key.
        """
        if row.api_key_encrypted is None:
            if row.provider in _NULL_KEY_TOLERANT_PROVIDERS:
                return ""
            raise ValueError(
                f"ModelAccount {row.id} has no api key to reveal (provider={row.provider!r})"
            )
        return self._cipher.decrypt(row.api_key_encrypted)
