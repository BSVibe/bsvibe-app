"""ChatService — OpenAI-shape chat completions dispatcher (Lift E2).

Thin coordinator that the external OpenAI-compatible proxy
(:mod:`backend.api.v1.chat`) calls. The caller passes
``model_account_id`` explicitly, so routing is trivial — no resolver,
no classifier. The service decrypts the account's credentials, runs the
LLM call through :class:`backend.router.llm_client.LlmClient`, and
returns the OpenAI-shape completion. Budget tracking still applies; the
budget-exceeded path is what made the legacy ``GatewayDispatcher``
asymmetric with the rest of the call sites (which use the resolver) —
keeping it here keeps the per-account budget invariant intact for the
public proxy.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from backend.router.accounts.crypto import CredentialCipher, _key_from_settings
from backend.router.accounts.service import ModelAccountService
from backend.router.budget.policy import BudgetPolicyService
from backend.router.dispatch import ModelAccountNotFound
from backend.router.llm_client import LlmClient

logger = structlog.get_logger(__name__)


@dataclass
class ChatCompletionContext:
    workspace_id: uuid.UUID
    account_id: uuid.UUID | None
    trace_id: str
    stream: bool
    model_account_id: uuid.UUID | None = None
    estimated_cost_cents: int = 0


_COST_PER_TOKEN_CENTS = 0.002


class ChatService:
    """OpenAI-compatible chat completions dispatcher (no classifier)."""

    def __init__(
        self,
        *,
        session: AsyncSession,
        budget: BudgetPolicyService,
        accounts: ModelAccountService | None = None,
        llm: LlmClient | None = None,
        cipher: CredentialCipher | None = None,
    ) -> None:
        self._session = session
        self._budget = budget
        self._cipher = cipher or CredentialCipher(_key_from_settings())
        self._accounts = accounts or ModelAccountService(session, cipher=self._cipher)
        self._llm = llm or LlmClient()

    async def complete(
        self,
        *,
        context: ChatCompletionContext,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Non-streaming dispatch. Returns the OpenAI-shape completion dict."""
        if context.account_id is None or context.model_account_id is None:
            raise ValueError("account_id and model_account_id are required for dispatch")

        row = await self._accounts._repo.get(  # noqa: SLF001 — same package
            workspace_id=context.workspace_id,
            account_id=context.account_id,
            model_account_id=context.model_account_id,
        )
        if row is None or not row.is_active:
            raise ModelAccountNotFound(
                f"ModelAccount {context.model_account_id} not found / inactive "
                f"under workspace={context.workspace_id} account={context.account_id}"
            )

        budget_check = await self._budget.check_request_cost(
            workspace_id=context.workspace_id,
            account_id=context.account_id,
            projected_cost_cents=context.estimated_cost_cents,
        )

        messages = payload.get("messages", [])
        api_key = self._accounts.reveal_api_key(row)
        response = await self._llm.chat(
            model=row.litellm_model,
            messages=list(messages),
            api_base=row.api_base,
            api_key=api_key,
            extra_params=dict(row.extra_params),
            tools=None,
        )

        actual_tokens = response.usage_prompt_tokens + response.usage_completion_tokens
        actual_cost_cents = int(round(actual_tokens * _COST_PER_TOKEN_CENTS))
        await self._budget.record_actual_cost(
            workspace_id=context.workspace_id,
            account_id=context.account_id,
            cost_cents=actual_cost_cents,
        )

        logger.info(
            "chat_completion_dispatched",
            workspace_id=str(context.workspace_id),
            trace_id=context.trace_id,
            model=row.litellm_model,
            actual_cost_cents=actual_cost_cents,
            breached_scopes=budget_check.breached_scopes,
        )
        return {
            "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
            "object": "chat.completion",
            "model": payload.get("model", ""),
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": response.content},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": response.usage_prompt_tokens,
                "completion_tokens": response.usage_completion_tokens,
                "total_tokens": actual_tokens,
            },
            "bsvibe": {
                "actual_cost_cents": actual_cost_cents,
            },
        }

    async def stream(
        self,
        *,
        context: ChatCompletionContext,
        payload: dict[str, Any],
    ) -> AsyncIterator[dict[str, Any]]:
        """Streaming dispatch — emits the completion as a single chunk."""
        completion = await self.complete(context=context, payload=payload)
        yield completion


__all__ = ["ChatCompletionContext", "ChatService"]
