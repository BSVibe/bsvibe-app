"""Redis-backed per-account cost tracker.

Key format::

    ws:{workspace_id}:acct:{account_id}:cost:{daily|monthly}:{period_key}

``period_key`` is ``YYYY-MM-DD`` for daily and ``YYYY-MM`` for monthly.
Values are stored as integer cents so we can use INCRBY atomically.
Test code passes a fake Redis (or :class:`InMemoryBudgetStore`) so we
don't need a live cluster in unit tests.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Protocol


class BudgetStore(Protocol):
    async def incrby(self, key: str, amount: int) -> int: ...

    async def get_int(self, key: str) -> int: ...


class InMemoryBudgetStore:
    """A trivial dict-backed store — for tests + dev without Redis."""

    def __init__(self) -> None:
        self._data: dict[str, int] = {}

    async def incrby(self, key: str, amount: int) -> int:
        self._data[key] = self._data.get(key, 0) + amount
        return self._data[key]

    async def get_int(self, key: str) -> int:
        return self._data.get(key, 0)


def _today(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).strftime("%Y-%m-%d")


def _this_month(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).strftime("%Y-%m")


class BudgetTracker:
    def __init__(self, store: BudgetStore) -> None:
        self._store = store

    @staticmethod
    def _key(
        *,
        workspace_id: uuid.UUID,
        account_id: uuid.UUID,
        scope: str,
        period: str,
    ) -> str:
        return f"ws:{workspace_id}:acct:{account_id}:cost:{scope}:{period}"

    async def record_cost(
        self,
        *,
        workspace_id: uuid.UUID,
        account_id: uuid.UUID,
        cost_cents: int,
        now: datetime | None = None,
    ) -> None:
        await self._store.incrby(
            self._key(
                workspace_id=workspace_id,
                account_id=account_id,
                scope="daily",
                period=_today(now),
            ),
            cost_cents,
        )
        await self._store.incrby(
            self._key(
                workspace_id=workspace_id,
                account_id=account_id,
                scope="monthly",
                period=_this_month(now),
            ),
            cost_cents,
        )

    async def daily_cost(
        self,
        *,
        workspace_id: uuid.UUID,
        account_id: uuid.UUID,
        now: datetime | None = None,
    ) -> int:
        return await self._store.get_int(
            self._key(
                workspace_id=workspace_id,
                account_id=account_id,
                scope="daily",
                period=_today(now),
            )
        )

    async def monthly_cost(
        self,
        *,
        workspace_id: uuid.UUID,
        account_id: uuid.UUID,
        now: datetime | None = None,
    ) -> int:
        return await self._store.get_int(
            self._key(
                workspace_id=workspace_id,
                account_id=account_id,
                scope="monthly",
                period=_this_month(now),
            )
        )
