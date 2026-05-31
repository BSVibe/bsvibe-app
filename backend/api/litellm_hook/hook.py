"""LiteLLM ``async_pre_call_hook`` — rule eval + budget + account resolve.

Per-request orchestrator wired against:

* :class:`backend.router.rules.RuleEngine.evaluate` for routing
* :class:`backend.router.budget.BudgetPolicyService.check_request_cost` for caps

The hook stays a thin coordinator; rules and budgets each own their domain
logic. Concrete repository / classifier / audit wiring lands when Bundle G
provides the request-scoped ``AsyncSession`` (see ``HookDependencies``).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import structlog

from backend.router.budget.errors import BudgetExceeded
from backend.router.budget.policy import BudgetPolicyService
from backend.router.rules.engine import RuleEngine
from backend.router.rules.models import RoutingRule, RuleMatch

logger = structlog.get_logger(__name__)


@dataclass
class HookContext:
    """Per-request identity context."""

    workspace_id: uuid.UUID
    account_id: uuid.UUID | None
    user_id: uuid.UUID | None
    trace_id: str


@dataclass
class HookDependencies:
    """Per-request services the hook needs.

    Bundle G integration constructs this from the request-scoped AsyncSession +
    cached singletons. For unit tests, fake the two services + pass the rules
    list directly.
    """

    rule_engine: RuleEngine
    rules: list[RoutingRule]
    budget_service: BudgetPolicyService | None = None
    estimated_cost_cents: int = 0


class LiteLLMHook:
    """Pre-call hook bound to a single request scope."""

    def __init__(self, *, context: HookContext, deps: HookDependencies) -> None:
        self._context = context
        self._deps = deps

    async def async_pre_call_hook(
        self,
        data: dict[str, Any],
        **_kwargs: Any,
    ) -> dict[str, Any]:
        """Evaluate routing rules + check budget. Returns possibly-mutated ``data``.

        Raises:
            :class:`backend.router.budget.errors.BudgetExceeded` if a budget
                policy is breached with ``enforcement="block"``. Callers
                translate this into HTTP 429.

        TODO(bundle-api-integration): the remaining call-sites (account
        resolution, classifier secondary tier, routing-log insert, audit
        emit) need a request-scoped AsyncSession that Bundle G threads in.
        """
        ctx = self._context
        if ctx.account_id is None:
            # No account scoping — rules + budget are account-keyed; pass through.
            logger.debug(
                "litellm_hook_no_account",
                workspace_id=str(ctx.workspace_id),
                trace_id=ctx.trace_id,
            )
            return data

        match: RuleMatch | None = await self._deps.rule_engine.evaluate(
            data,
            rules=self._deps.rules,
            workspace_id=ctx.workspace_id,
            account_id=ctx.account_id,
        )
        if match is not None and match.target_model:
            data["model"] = match.target_model
            logger.info(
                "litellm_hook_rule_matched",
                workspace_id=str(ctx.workspace_id),
                rule_name=match.rule.name,
                target_model=match.target_model,
                trace_id=ctx.trace_id,
            )

        if self._deps.budget_service is not None:
            result = await self._deps.budget_service.check_request_cost(
                workspace_id=ctx.workspace_id,
                account_id=ctx.account_id,
                projected_cost_cents=self._deps.estimated_cost_cents,
            )
            if result.blocked:
                scope = result.breached_scopes[0] if result.breached_scopes else "unknown"
                current = (
                    result.daily_current_cents if scope == "daily" else result.monthly_current_cents
                )
                raise BudgetExceeded(
                    scope=scope,
                    current_cents=current,
                    cap_cents=0,  # repository lookup happens inside the service
                )

        return data
