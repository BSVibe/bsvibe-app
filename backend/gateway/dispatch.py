"""Gateway dispatch entry point — multi-account, budget-aware.

A request arrives with (workspace_id, account_id, messages, features).
The dispatch entry:

1. Loads the ``ModelAccount`` row for that account.
2. Asks the :class:`Classifier` for a tier verdict (local vs cloud).
3. Asks :class:`BudgetPolicyService` to check the projected cost.
4. On approval, hands off to :class:`LlmClient` with the account's
   decrypted credentials.
5. Records the actual cost into the budget tracker so future calls see
   the running total.

This is intentionally a thin orchestration layer; the heavy lifting
lives in the underlying components so each can be unit-tested in
isolation.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import structlog

from backend.gateway.accounts.service import ModelAccountService
from backend.gateway.budget.errors import BudgetExceeded
from backend.gateway.budget.policy import BudgetPolicyService
from backend.gateway.classifier.base import (
    ClassificationFeatures,
    ClassificationResult,
    Classifier,
)
from backend.gateway.llm_client import LlmClient, LlmResponse

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class DispatchRequest:
    workspace_id: uuid.UUID
    account_id: uuid.UUID
    model_account_id: uuid.UUID
    messages: list[dict[str, object]]
    features: ClassificationFeatures
    projected_cost_cents: int


@dataclass(frozen=True)
class DispatchResult:
    classification: ClassificationResult
    response: LlmResponse
    actual_cost_cents: int


class DispatchError(RuntimeError):
    """Catch-all for unrecoverable dispatch errors."""


class ModelAccountNotFound(DispatchError):
    """The (workspace, account, model_account) row is missing."""


class GatewayDispatcher:
    def __init__(
        self,
        *,
        accounts: ModelAccountService,
        classifier: Classifier,
        budget: BudgetPolicyService,
        llm: LlmClient,
        cost_per_token_cents: float = 0.002,
    ) -> None:
        self._accounts = accounts
        self._classifier = classifier
        self._budget = budget
        self._llm = llm
        self._cost_per_token_cents = cost_per_token_cents

    async def dispatch(self, req: DispatchRequest) -> DispatchResult:
        # 1. Load the model account row + reveal credentials.
        row = await self._accounts._repo.get(  # noqa: SLF001 — same package
            workspace_id=req.workspace_id,
            account_id=req.account_id,
            model_account_id=req.model_account_id,
        )
        if row is None or not row.is_active:
            raise ModelAccountNotFound(
                f"ModelAccount {req.model_account_id} not found / inactive "
                f"under workspace={req.workspace_id} account={req.account_id}"
            )

        # 2. Budget check up front (raises BudgetExceeded if hard-blocked).
        budget_check = await self._budget.check_request_cost(
            workspace_id=req.workspace_id,
            account_id=req.account_id,
            projected_cost_cents=req.projected_cost_cents,
        )

        # 3. Classifier (informational at the moment — tier is reported
        #    back to the caller so the orchestrator can swap models;
        #    later bundles plumb this into RuleEngine.evaluate()).
        classification = await self._classifier.classify(req.features)

        # 4. Dispatch the call with the account's decrypted credentials.
        api_key = self._accounts.reveal_api_key(row)
        response = await self._llm.chat(
            model=row.litellm_model,
            messages=list(req.messages),
            api_base=row.api_base,
            api_key=api_key,
            extra_params=dict(row.extra_params),
        )

        # 5. Record the actual cost (rounded to nearest cent).
        actual_tokens = response.usage_prompt_tokens + response.usage_completion_tokens
        actual_cost_cents = int(round(actual_tokens * self._cost_per_token_cents))
        await self._budget.record_actual_cost(
            workspace_id=req.workspace_id,
            account_id=req.account_id,
            cost_cents=actual_cost_cents,
        )

        logger.info(
            "gateway_dispatch_complete",
            workspace_id=str(req.workspace_id),
            account_id=str(req.account_id),
            model=row.litellm_model,
            tier=classification.tier,
            tokens=actual_tokens,
            actual_cost_cents=actual_cost_cents,
            breached_scopes=budget_check.breached_scopes,
        )

        return DispatchResult(
            classification=classification,
            response=response,
            actual_cost_cents=actual_cost_cents,
        )


__all__ = [
    "BudgetExceeded",
    "DispatchError",
    "DispatchRequest",
    "DispatchResult",
    "GatewayDispatcher",
    "ModelAccountNotFound",
]
