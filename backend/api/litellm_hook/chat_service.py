"""ChatService — OpenAI-shape chat completions dispatcher.

Thin coordinator over :class:`backend.gateway.dispatch.GatewayDispatcher`.
Caller constructs the dispatcher (with its concrete deps: ModelAccountService,
Classifier, BudgetPolicyService, LlmClient) and hands it to the service for
the lifetime of one request.

Streaming uses the same dispatcher's underlying ``LlmClient.chat_stream`` if
available; otherwise it falls back to one-shot ``complete`` + a single
SSE-style chunk so the client still sees the OpenAI streaming shape.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import structlog

from backend.gateway.classifier.base import ClassificationFeatures
from backend.gateway.dispatch import DispatchRequest, GatewayDispatcher

logger = structlog.get_logger(__name__)


@dataclass
class ChatCompletionContext:
    workspace_id: uuid.UUID
    account_id: uuid.UUID | None
    trace_id: str
    stream: bool
    model_account_id: uuid.UUID | None = None
    estimated_cost_cents: int = 0


class ChatService:
    """OpenAI-compatible chat completions dispatcher."""

    def __init__(self, *, dispatcher: GatewayDispatcher | None = None) -> None:
        self._dispatcher = dispatcher

    async def complete(
        self,
        *,
        context: ChatCompletionContext,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Non-streaming dispatch. Returns the OpenAI-shape completion dict."""
        if self._dispatcher is None:
            raise RuntimeError(
                "ChatService.complete requires a GatewayDispatcher — pass it "
                "into the ChatService constructor."
            )
        if context.account_id is None or context.model_account_id is None:
            raise ValueError("account_id and model_account_id are required for dispatch")

        messages = payload.get("messages", [])
        features = _features_from_messages(messages)
        request = DispatchRequest(
            workspace_id=context.workspace_id,
            account_id=context.account_id,
            model_account_id=context.model_account_id,
            messages=messages,
            features=features,
            projected_cost_cents=context.estimated_cost_cents,
        )
        result = await self._dispatcher.dispatch(request)
        logger.info(
            "chat_completion_dispatched",
            workspace_id=str(context.workspace_id),
            trace_id=context.trace_id,
            classification_tier=result.classification.tier,
            actual_cost_cents=result.actual_cost_cents,
        )
        return {
            "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
            "object": "chat.completion",
            "model": result.response.model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": result.response.content},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": result.response.prompt_tokens,
                "completion_tokens": result.response.completion_tokens,
                "total_tokens": result.response.prompt_tokens + result.response.completion_tokens,
            },
            "bsvibe": {
                "classification_tier": result.classification.tier,
                "actual_cost_cents": result.actual_cost_cents,
            },
        }

    async def stream(
        self,
        *,
        context: ChatCompletionContext,
        payload: dict[str, Any],
    ) -> AsyncIterator[dict[str, Any]]:
        """Streaming dispatch — yields SSE-shape chunks.

        Phase 1: emits the non-streaming result as a single chunk + a
        terminal ``data: [DONE]`` marker. Full token-stream lift is
        Bundle G follow-up.
        """
        completion = await self.complete(context=context, payload=payload)
        yield completion


def _features_from_messages(messages: list[dict[str, Any]]) -> ClassificationFeatures:
    """Derive informational classifier features from an OpenAI-shape messages list."""
    user_parts: list[str] = []
    system_parts: list[str] = []
    code_block_count = 0
    for m in messages:
        content = m.get("content", "")
        if not isinstance(content, str):
            continue
        if m.get("role") == "system":
            system_parts.append(content)
        elif m.get("role") == "user":
            user_parts.append(content)
        code_block_count += content.count("```") // 2
    user_text = "\n".join(user_parts)
    system_prompt = "\n".join(system_parts)
    return ClassificationFeatures(
        token_count=len(user_text.split()) + len(system_prompt.split()),
        system_prompt_chars=len(system_prompt),
        conversation_turns=sum(1 for m in messages if m.get("role") == "user"),
        code_block_count=code_block_count,
        tool_count=0,
        user_text=user_text,
        system_prompt=system_prompt,
    )
