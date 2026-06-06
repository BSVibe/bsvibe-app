"""SqlAlchemyModelAccountRepository — concrete :class:`ModelAccountRepository`.

v8 D44/D45 — infrastructure-layer SQL adapter for the Router context's
ModelAccount aggregate. Folds in the queries the legacy
:mod:`backend.router.accounts.repository` shipped today PLUS the
``list_active_for_workspace`` query the run resolver needs (which the
legacy class never exposed — workflow/infrastructure/workers/run.py issued
the raw ``select(ModelAccount)`` inline, the violation this lift removes).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.router.accounts.models import ModelAccount
from backend.router.accounts.predicates import EXECUTOR_PROVIDER


class SqlAlchemyModelAccountRepository:
    """SQLAlchemy-backed :class:`ModelAccountRepository`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        workspace_id: uuid.UUID,
        account_id: uuid.UUID,
        provider: str,
        label: str,
        litellm_model: str,
        api_base: str | None,
        api_key_encrypted: str | None,
        data_jurisdiction: str,
        extra_params: dict[str, Any],
    ) -> ModelAccount:
        row = ModelAccount(
            workspace_id=workspace_id,
            account_id=account_id,
            provider=provider,
            label=label,
            litellm_model=litellm_model,
            api_base=api_base,
            api_key_encrypted=api_key_encrypted,
            data_jurisdiction=data_jurisdiction,
            extra_params=extra_params,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def get(
        self,
        *,
        workspace_id: uuid.UUID,
        account_id: uuid.UUID,
        model_account_id: uuid.UUID,
    ) -> ModelAccount | None:
        stmt = select(ModelAccount).where(
            ModelAccount.id == model_account_id,
            ModelAccount.workspace_id == workspace_id,
            ModelAccount.account_id == account_id,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_for_account(
        self,
        *,
        workspace_id: uuid.UUID,
        account_id: uuid.UUID,
        only_active: bool = False,
    ) -> Sequence[ModelAccount]:
        stmt = select(ModelAccount).where(
            ModelAccount.workspace_id == workspace_id,
            ModelAccount.account_id == account_id,
            ModelAccount.provider != EXECUTOR_PROVIDER,
        )
        if only_active:
            stmt = stmt.where(ModelAccount.is_active.is_(True))
        stmt = stmt.order_by(ModelAccount.created_at.asc())
        return (await self._session.execute(stmt)).scalars().all()

    async def list_active_for_workspace(self, *, workspace_id: uuid.UUID) -> Sequence[ModelAccount]:
        stmt = (
            select(ModelAccount)
            .where(
                ModelAccount.workspace_id == workspace_id,
                ModelAccount.is_active.is_(True),
            )
            .order_by(ModelAccount.created_at.asc())
        )
        return (await self._session.execute(stmt)).scalars().all()

    async def list_executor_accounts_for_worker(
        self, *, workspace_id: uuid.UUID, worker_id: uuid.UUID
    ) -> Sequence[ModelAccount]:
        # Filtering on the JSON tag in Python keeps it portable across the
        # SQLite test tier and Postgres (JSON vs JSONB operator differences).
        stmt = select(ModelAccount).where(
            ModelAccount.workspace_id == workspace_id,
            ModelAccount.provider == EXECUTOR_PROVIDER,
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        target = str(worker_id)
        return [r for r in rows if r.extra_params.get("worker_id") == target]

    async def delete(
        self,
        *,
        workspace_id: uuid.UUID,
        account_id: uuid.UUID,
        model_account_id: uuid.UUID,
    ) -> bool:
        row = await self.get(
            workspace_id=workspace_id,
            account_id=account_id,
            model_account_id=model_account_id,
        )
        if row is None:
            return False
        await self._session.delete(row)
        await self._session.flush()
        return True

    async def update(self, row: ModelAccount, **fields: Any) -> ModelAccount:
        for k, v in fields.items():
            if v is not None:
                setattr(row, k, v)
        await self._session.flush()
        return row


__all__ = ["SqlAlchemyModelAccountRepository"]
