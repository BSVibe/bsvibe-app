"""SQL CRUD for AccountBudgetPolicy."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.router.budget.models import (
    AccountBudgetPolicy,
    BudgetEnforcement,
    BudgetScope,
)


class BudgetPolicyRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert(
        self,
        *,
        workspace_id: uuid.UUID,
        account_id: uuid.UUID,
        scope: BudgetScope,
        cost_cap_cents: int,
        enforcement: BudgetEnforcement = BudgetEnforcement.BLOCK,
    ) -> AccountBudgetPolicy:
        existing = await self.get(workspace_id=workspace_id, account_id=account_id, scope=scope)
        if existing is not None:
            existing.cost_cap_cents = cost_cap_cents
            existing.enforcement = enforcement
            await self._session.flush()
            return existing

        row = AccountBudgetPolicy(
            workspace_id=workspace_id,
            account_id=account_id,
            scope=scope,
            cost_cap_cents=cost_cap_cents,
            enforcement=enforcement,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def get(
        self,
        *,
        workspace_id: uuid.UUID,
        account_id: uuid.UUID,
        scope: BudgetScope,
    ) -> AccountBudgetPolicy | None:
        stmt = select(AccountBudgetPolicy).where(
            AccountBudgetPolicy.workspace_id == workspace_id,
            AccountBudgetPolicy.account_id == account_id,
            AccountBudgetPolicy.scope == scope,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_(
        self,
        *,
        workspace_id: uuid.UUID,
        account_id: uuid.UUID,
    ) -> Sequence[AccountBudgetPolicy]:
        stmt = select(AccountBudgetPolicy).where(
            AccountBudgetPolicy.workspace_id == workspace_id,
            AccountBudgetPolicy.account_id == account_id,
        )
        return (await self._session.execute(stmt)).scalars().all()

    async def delete(
        self,
        *,
        workspace_id: uuid.UUID,
        account_id: uuid.UUID,
        scope: BudgetScope,
    ) -> bool:
        row = await self.get(workspace_id=workspace_id, account_id=account_id, scope=scope)
        if row is None:
            return False
        await self._session.delete(row)
        await self._session.flush()
        return True
