"""Gateway dispatcher + cheap-LLM seam adapters (§17.2a slice).

Three closely-coupled pieces extracted out of the legacy
``backend.workflow.infrastructure.workers.run`` god-file:

* :func:`build_gateway_dispatcher` — constructs the per-session
  :class:`GatewayDispatcher` exactly as ``backend.api.v1.chat`` does
  (intentionally NOT factored across the HTTP/worker boundary, so each
  entrypoint's wiring stays explicit).
* :class:`_GatewayCompileLlm` — adapts the dispatcher to BSage's
  ``CompileLlm`` seam (settle entity extraction, a single chat call).
* :class:`_GatewayFrameLlm` — adapts the dispatcher to the
  :class:`FrameLlm` seam (B9a — the frame stage's cheap completion).

All three live together because they share the dispatcher build pattern
+ the workspace/account/model-account identity triple; co-locating keeps
the per-stage features matrix in one place.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import Settings
from backend.router.accounts.crypto import CredentialCipher, _key_from_settings
from backend.router.accounts.service import ModelAccountService
from backend.router.budget.policy import BudgetPolicyService
from backend.router.budget.repository import BudgetPolicyRepository
from backend.router.budget.tracker import BudgetTracker, InMemoryBudgetStore
from backend.router.classifier.base import ClassificationFeatures
from backend.router.classifier.local_vs_cloud import LocalVsCloudClassifier
from backend.router.classifier.static import StaticClassifier
from backend.router.dispatch import DispatchRequest, GatewayDispatcher
from backend.router.llm_client import LlmClient


def build_gateway_dispatcher(session: AsyncSession, settings: Settings) -> GatewayDispatcher:
    """Construct a :class:`GatewayDispatcher` exactly as the HTTP chat path does.

    The work-LLM (:class:`GatewayLoopLlm`) routes every plan/act/judge turn
    through this dispatcher; it resolves the account + model + budget and hands
    off to :class:`LlmClient`. Built per-session so compute shares the run's
    transaction. (Mirrors ``backend.api.v1.chat._build_dispatcher`` —
    intentionally NOT factored out across the HTTP/worker boundary to keep each
    entrypoint's wiring explicit.)"""
    cipher = CredentialCipher(_key_from_settings())
    accounts = ModelAccountService(session, cipher=cipher)
    budget_repo = BudgetPolicyRepository(session)
    tracker = BudgetTracker(InMemoryBudgetStore())
    budget = BudgetPolicyService(repository=budget_repo, tracker=tracker)
    classifier = LocalVsCloudClassifier(
        local_score_max=settings.gateway_local_score_max,
        cloud_score_min=settings.gateway_cloud_score_min,
        static=StaticClassifier(
            local_score_max=settings.gateway_local_score_max,
            cloud_score_min=settings.gateway_cloud_score_min,
        ),
    )
    llm = LlmClient()
    return GatewayDispatcher(accounts=accounts, classifier=classifier, budget=budget, llm=llm)


class _GatewayCompileLlm:
    """Adapts :class:`GatewayDispatcher` to the ``CompileLlm`` seam.

    Maps a single ``chat(system, messages, ...)`` call to a ``DispatchRequest``
    and returns the response content string. The account/model identity is
    resolved once (per workspace) by the factory and held for the call. Mirrors
    :class:`~backend.workflow.application.loop_llm.GatewayLoopLlm`, but for the
    plain chat-completion (no tools) extraction call."""

    # Substantial-tier features — extraction is a structured-output task that
    # benefits from the heavier model, same as the agent loop's plan/act turns.
    _FEATURES = ClassificationFeatures(
        token_count=4096,
        system_prompt_chars=2048,
        conversation_turns=1,
        code_block_count=0,
        tool_count=0,
    )

    def __init__(
        self,
        *,
        dispatcher: GatewayDispatcher,
        workspace_id: uuid.UUID,
        account_id: uuid.UUID,
        model_account_id: uuid.UUID,
    ) -> None:
        self._dispatcher = dispatcher
        self._workspace_id = workspace_id
        self._account_id = account_id
        self._model_account_id = model_account_id

    async def chat(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        suppress_reasoning: bool = False,
        timeout_s: float | None = None,
    ) -> str:
        # CompileLlm passes only user messages; the system prompt is a separate
        # arg — prepend it so the dispatcher (OpenAI-style messages) sees it.
        full_messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
        full_messages.extend(dict(m) for m in messages)
        request = DispatchRequest(
            workspace_id=self._workspace_id,
            account_id=self._account_id,
            model_account_id=self._model_account_id,
            messages=full_messages,
            features=self._FEATURES,
            projected_cost_cents=1,
        )
        result = await self._dispatcher.dispatch(request)
        return result.response.content


class _GatewayFrameLlm:
    """Adapts :class:`GatewayDispatcher` to the :class:`FrameLlm` seam (B9a).

    The frame stage is a single cheap completion: ``complete_text(system, user)``
    maps to one :class:`DispatchRequest`. Framing is a small classification call,
    so it uses LIGHTER features than the work loop (it benefits from the cheap
    tier — Workflow §1.2 "✓ cheap"). The account/model identity is resolved once
    (per workspace) by the factory and held for the call."""

    # Cheap-tier features — framing is a short interpret/classify call, not the
    # heavy structured-output of the work loop, so it deliberately routes cheaper.
    _FEATURES = ClassificationFeatures(
        token_count=512,
        system_prompt_chars=1024,
        conversation_turns=1,
        code_block_count=0,
        tool_count=0,
    )

    def __init__(
        self,
        *,
        dispatcher: GatewayDispatcher,
        workspace_id: uuid.UUID,
        account_id: uuid.UUID,
        model_account_id: uuid.UUID,
    ) -> None:
        self._dispatcher = dispatcher
        self._workspace_id = workspace_id
        self._account_id = account_id
        self._model_account_id = model_account_id

    async def complete_text(self, *, system: str, user: str) -> str:
        request = DispatchRequest(
            workspace_id=self._workspace_id,
            account_id=self._account_id,
            model_account_id=self._model_account_id,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            features=self._FEATURES,
            projected_cost_cents=1,
        )
        result = await self._dispatcher.dispatch(request)
        return result.response.content


__all__ = [
    "_GatewayCompileLlm",
    "_GatewayFrameLlm",
    "build_gateway_dispatcher",
]
