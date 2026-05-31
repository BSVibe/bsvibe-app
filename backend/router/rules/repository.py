"""SQL CRUD for routing rules + conditions, account-scoped.

Pattern matches :mod:`backend.router.budget.repository` — bare
``AsyncSession`` constructor, plain ``select`` / ``update`` /
``delete``, no SQL files. Optional :class:`RulesCache` invalidates on
write; reads bypass the cache (callers that need cached reads use the
dispatch layer, not the repository directly).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.router.rules.cache import RulesCache
from backend.router.rules.db import RoutingRuleRow, RuleConditionRow


class RuleDuplicateError(Exception):
    """Raised when a rule violates ``(workspace_id, account_id, name)``
    or the deferrable priority unique constraint."""


class RulesRepository:
    def __init__(
        self,
        session: AsyncSession,
        cache: RulesCache | None = None,
    ) -> None:
        self._session = session
        self._cache = cache

    # ----- rules -----

    async def create_rule(
        self,
        *,
        workspace_id: uuid.UUID,
        account_id: uuid.UUID,
        name: str,
        priority: int,
        target_model: str,
        is_active: bool = True,
        is_default: bool = False,
    ) -> RoutingRuleRow:
        row = RoutingRuleRow(
            workspace_id=workspace_id,
            account_id=account_id,
            name=name,
            priority=priority,
            target_model=target_model,
            is_active=is_active,
            is_default=is_default,
        )
        self._session.add(row)
        try:
            await self._session.flush()
        except IntegrityError as exc:
            await self._session.rollback()
            raise RuleDuplicateError(str(exc.orig)) from exc
        await self._invalidate(workspace_id, account_id)
        return row

    async def get_rule(
        self,
        rule_id: uuid.UUID,
        *,
        workspace_id: uuid.UUID,
        account_id: uuid.UUID,
    ) -> RoutingRuleRow | None:
        stmt = select(RoutingRuleRow).where(
            RoutingRuleRow.id == rule_id,
            RoutingRuleRow.workspace_id == workspace_id,
            RoutingRuleRow.account_id == account_id,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_rules(
        self,
        *,
        workspace_id: uuid.UUID,
        account_id: uuid.UUID,
    ) -> Sequence[RoutingRuleRow]:
        stmt = (
            select(RoutingRuleRow)
            .where(
                RoutingRuleRow.workspace_id == workspace_id,
                RoutingRuleRow.account_id == account_id,
            )
            .order_by(RoutingRuleRow.priority.asc())
        )
        return (await self._session.execute(stmt)).scalars().all()

    async def update_rule(
        self,
        rule_id: uuid.UUID,
        *,
        workspace_id: uuid.UUID,
        account_id: uuid.UUID,
        name: str,
        priority: int,
        is_default: bool,
        target_model: str,
        is_active: bool = True,
    ) -> RoutingRuleRow | None:
        row = await self.get_rule(rule_id, workspace_id=workspace_id, account_id=account_id)
        if row is None:
            return None
        row.name = name
        row.priority = priority
        row.is_default = is_default
        row.target_model = target_model
        row.is_active = is_active
        try:
            await self._session.flush()
        except IntegrityError as exc:
            await self._session.rollback()
            raise RuleDuplicateError(str(exc.orig)) from exc
        await self._invalidate(workspace_id, account_id)
        return row

    async def delete_rule(
        self,
        rule_id: uuid.UUID,
        *,
        workspace_id: uuid.UUID,
        account_id: uuid.UUID,
    ) -> bool:
        row = await self.get_rule(rule_id, workspace_id=workspace_id, account_id=account_id)
        if row is None:
            return False
        await self._session.delete(row)
        await self._session.flush()
        await self._invalidate(workspace_id, account_id)
        return True

    async def reorder_rules(
        self,
        *,
        workspace_id: uuid.UUID,
        account_id: uuid.UUID,
        priorities: dict[uuid.UUID, int],
    ) -> None:
        """Apply many priority updates atomically.

        Two-phase to avoid intermediate-state UNIQUE collisions:

        1. Park the affected rules at negative placeholder priorities
           (no live rule ever has ``priority < 1``).
        2. Apply the target priorities.

        Works on both Postgres (where the unique is DEFERRABLE INITIALLY
        DEFERRED anyway) and SQLite (which has no DEFERRABLE support, so
        relies on this park step).
        """
        if not priorities:
            return
        scope = (
            RoutingRuleRow.workspace_id == workspace_id,
            RoutingRuleRow.account_id == account_id,
        )
        # Phase 1 — park.
        for offset, rule_id in enumerate(priorities.keys(), start=1):
            await self._session.execute(
                update(RoutingRuleRow)
                .where(RoutingRuleRow.id == rule_id, *scope)
                .values(priority=-offset)
            )
        # Phase 2 — assign targets.
        for rule_id, prio in priorities.items():
            await self._session.execute(
                update(RoutingRuleRow)
                .where(RoutingRuleRow.id == rule_id, *scope)
                .values(priority=prio)
            )
        await self._session.flush()
        await self._invalidate(workspace_id, account_id)

    # ----- conditions -----

    async def list_conditions(self, rule_id: uuid.UUID) -> Sequence[RuleConditionRow]:
        stmt = select(RuleConditionRow).where(RuleConditionRow.rule_id == rule_id)
        return (await self._session.execute(stmt)).scalars().all()

    async def replace_conditions(
        self,
        rule_id: uuid.UUID,
        conditions: list[dict[str, Any]],
    ) -> None:
        await self._session.execute(
            delete(RuleConditionRow).where(RuleConditionRow.rule_id == rule_id)
        )
        for c in conditions:
            self._session.add(
                RuleConditionRow(
                    rule_id=rule_id,
                    condition_type=c["condition_type"],
                    operator=c.get("operator", "eq"),
                    field=c["field"],
                    value=c["value"],
                    negate=c.get("negate", False),
                )
            )
        await self._session.flush()

    # ----- internals -----

    async def _invalidate(self, workspace_id: uuid.UUID, account_id: uuid.UUID) -> None:
        if self._cache is not None:
            await self._cache.invalidate(workspace_id, account_id)
