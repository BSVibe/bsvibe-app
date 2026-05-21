"""BudgetPolicyService — per-account, per-scope cap evaluation.

Composed around the SQL :class:`BudgetPolicyRepository` (cap config) +
the Redis-ish :class:`BudgetTracker` (running spend). The dispatch path
calls :meth:`check_request_cost` before sending the LLM request; if the
projected total would exceed the cap and enforcement is ``block``,
:exc:`BudgetExceeded` is raised. ``warn`` / ``log`` modes return a
:class:`BudgetCheckResult` with the breach flag set but don't raise.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import structlog

from backend.gateway.budget.errors import BudgetExceeded
from backend.gateway.budget.models import BudgetEnforcement, BudgetScope
from backend.gateway.budget.repository import BudgetPolicyRepository
from backend.gateway.budget.tracker import BudgetTracker

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class BudgetCheckResult:
    """Outcome of one :meth:`BudgetPolicyService.check_request_cost` call."""

    blocked: bool
    breached_scopes: tuple[str, ...]
    daily_current_cents: int
    monthly_current_cents: int


class BudgetPolicyService:
    def __init__(
        self,
        *,
        repository: BudgetPolicyRepository,
        tracker: BudgetTracker,
    ) -> None:
        self._repo = repository
        self._tracker = tracker

    async def check_request_cost(
        self,
        *,
        workspace_id: uuid.UUID,
        account_id: uuid.UUID,
        projected_cost_cents: int,
    ) -> BudgetCheckResult:
        daily_now = await self._tracker.daily_cost(workspace_id=workspace_id, account_id=account_id)
        monthly_now = await self._tracker.monthly_cost(
            workspace_id=workspace_id, account_id=account_id
        )

        breached: list[str] = []
        block = False

        for scope, current in (
            (BudgetScope.DAILY, daily_now),
            (BudgetScope.MONTHLY, monthly_now),
        ):
            policy = await self._repo.get(
                workspace_id=workspace_id, account_id=account_id, scope=scope
            )
            if policy is None:
                continue
            projected_total = current + projected_cost_cents
            if projected_total <= policy.cost_cap_cents:
                continue
            breached.append(scope.value)
            logger.warning(
                "budget_breach",
                workspace_id=str(workspace_id),
                account_id=str(account_id),
                scope=scope.value,
                current_cents=current,
                projected_total_cents=projected_total,
                cap_cents=policy.cost_cap_cents,
                enforcement=policy.enforcement.value,
            )
            if policy.enforcement is BudgetEnforcement.BLOCK:
                block = True
                raise BudgetExceeded(
                    scope=scope.value,
                    current_cents=current,
                    cap_cents=policy.cost_cap_cents,
                )

        return BudgetCheckResult(
            blocked=block,
            breached_scopes=tuple(breached),
            daily_current_cents=daily_now,
            monthly_current_cents=monthly_now,
        )

    async def record_actual_cost(
        self,
        *,
        workspace_id: uuid.UUID,
        account_id: uuid.UUID,
        cost_cents: int,
    ) -> None:
        await self._tracker.record_cost(
            workspace_id=workspace_id,
            account_id=account_id,
            cost_cents=cost_cents,
        )
