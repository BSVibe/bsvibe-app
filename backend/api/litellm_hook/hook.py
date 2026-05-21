"""LiteLLM ``async_pre_call_hook`` — rule eval + budget + account resolve.

Phase 1 skeleton — Bundle G integration provides AsyncSession + worker
context so the hook can call into RuleEngine / BudgetPolicyService /
ModelAccountService / RoutingLogsRepository / supervisor.audit.

The hook itself stays a thin orchestrator; the heavy logic lives in the
already-lifted ``backend.gateway.*`` modules.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


@dataclass
class HookContext:
    """Per-request context the hook needs to make its decisions."""

    workspace_id: uuid.UUID
    account_id: uuid.UUID | None
    user_id: uuid.UUID | None
    trace_id: str


class LiteLLMHook:
    """Pre-call hook bound to a single request scope.

    Lift target (deferred): BSGateway ``bsgateway/routing/hook.py``. The
    public surface mirrors litellm's expected hook signature so a future
    full lift drops in without API change for callers.
    """

    def __init__(self, *, context: HookContext) -> None:
        self._context = context

    async def async_pre_call_hook(
        self,
        data: dict[str, Any],
        **_kwargs: Any,
    ) -> dict[str, Any]:
        """Evaluate routing rules, check budget, resolve account, log.

        Returns the (possibly mutated) ``data`` dict that LiteLLM passes
        to the upstream provider. Raises ``HTTPException(429)`` on budget
        exceeded.
        """
        # TODO(bundle-api-integration): full lift from bsgateway/routing/hook.py:
        # 1. backend.gateway.rules.RuleEngine.evaluate(data, ...) → RuleMatch
        # 2. backend.gateway.budget.BudgetPolicyService.check_request_cost
        # 3. backend.accounts.ModelAccountService.resolve(account_id)
        # 4. backend.gateway.classifier.LocalVsCloudClassifier.classify
        # 5. RoutingLogsRepository.insert_routing_log
        # 6. supervisor.audit.safe_emit("gateway.completion.dispatched", ...)
        logger.debug(
            "litellm_hook_pre_call_stub",
            workspace_id=str(self._context.workspace_id),
            trace_id=self._context.trace_id,
        )
        return data
