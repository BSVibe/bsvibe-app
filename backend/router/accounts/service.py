"""ModelAccountService — orchestrates encryption + repository CRUD.

The service is the public API the rest of the gateway (and eventually
the REST router) calls; never mutate :class:`ModelAccount` rows or
encrypted blobs from elsewhere.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from backend.router.accounts.crypto import CredentialCipher
from backend.router.accounts.models import ModelAccount
from backend.router.accounts.predicates import EXECUTOR_PROVIDER
from backend.router.accounts.schemas import (
    ModelAccountCreate,
    ModelAccountOut,
    ModelAccountUpdate,
)
from backend.router.egress import (
    EgressRefused,
    Resolver,
    check_completion_kwargs,
    resolve_host,
)
from backend.router.infrastructure.repositories import SqlAlchemyModelAccountRepository

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
    def __init__(
        self,
        session: AsyncSession,
        *,
        cipher: CredentialCipher,
        resolver: Resolver = resolve_host,
    ) -> None:
        self._repo = SqlAlchemyModelAccountRepository(session)
        self._cipher = cipher
        self._resolver = resolver

    async def _check_destination(
        self,
        *,
        provider: str,
        litellm_model: str,
        api_base: str | None,
        extra_params: dict[str, Any],
    ) -> None:
        """Refuse to SAVE an account the completion path would refuse to CALL.

        The call-time check in :class:`~backend.router.llm_client.LlmClient` is the guarantee;
        this answers while the founder can still fix it, instead of on a run that then crashes.
        Executor accounts are exempt: they dispatch to the founder's own worker and never reach
        ``api_base``. Raises :class:`~backend.router.egress.EgressRefused`.
        """
        if provider == EXECUTOR_PROVIDER:
            return
        from backend.config import get_settings  # noqa: PLC0415 — read per call, like the client

        kwargs: dict[str, Any] = {"model": litellm_model}
        if api_base:
            kwargs["api_base"] = api_base
        kwargs.update(extra_params)
        await check_completion_kwargs(
            kwargs,
            resolver=self._resolver,
            allowed_hosts=frozenset(get_settings().llm_egress_allowed_hosts),
        )

    async def create(
        self,
        *,
        workspace_id: uuid.UUID,
        account_id: uuid.UUID,
        payload: ModelAccountCreate,
    ) -> ModelAccountOut:
        await self._check_destination(
            provider=payload.provider,
            litellm_model=payload.litellm_model,
            api_base=payload.api_base,
            extra_params=dict(payload.extra_params),
        )
        encrypted = self._cipher.encrypt(payload.api_key)
        row = await self._repo.create(
            workspace_id=workspace_id,
            account_id=account_id,
            provider=payload.provider,
            label=payload.label,
            litellm_model=payload.litellm_model,
            api_base=payload.api_base,
            api_key_encrypted=encrypted,
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
        rows = await self._repo.list_for_account(
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
        await self._check_destination(
            provider=row.provider,
            litellm_model=fields.get("litellm_model", row.litellm_model),
            api_base=fields.get("api_base", row.api_base),
            extra_params=dict(fields.get("extra_params", row.extra_params) or {}),
        )
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


__all__ = ["DEFAULT_ACCOUNT_LABEL", "EgressRefused", "ModelAccountService"]
