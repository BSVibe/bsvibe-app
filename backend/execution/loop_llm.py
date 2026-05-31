"""GatewayLoopLlm — the production :class:`LoopLlm` for the agent loop.

Routes every plan/act/judge turn through the
:class:`~backend.router.dispatch.GatewayDispatcher` (substantial tier;
the gateway resolves the account + model + budget). The work loop in
:mod:`backend.execution.orchestrator` depends only on the ``LoopLlm``
Protocol, so this adapter and a deterministic test stub are
interchangeable.

The account / model identity is resolved by the caller (the HTTP
Direct-path wiring or the worker — the next chunk) and held for the
run's lifetime; this adapter just maps ``(messages, tools)`` →
``DispatchRequest`` → :class:`LoopTurn`.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from backend.execution.orchestrator import LoopToolCall, LoopTurn
from backend.router.classifier.base import ClassificationFeatures
from backend.router.dispatch import DispatchRequest, GatewayDispatcher

# Substantial-tier default features — high enough complexity that the
# classifier routes plan/act to the heavy model.
_SUBSTANTIAL_FEATURES = ClassificationFeatures(
    token_count=4096,
    system_prompt_chars=2048,
    conversation_turns=4,
    code_block_count=2,
    tool_count=6,
)


class GatewayLoopLlm:
    """Adapts :class:`GatewayDispatcher` to the ``LoopLlm`` Protocol."""

    def __init__(
        self,
        *,
        dispatcher: GatewayDispatcher,
        workspace_id: uuid.UUID,
        account_id: uuid.UUID,
        model_account_id: uuid.UUID,
        features: ClassificationFeatures | None = None,
        projected_cost_cents: int = 1,
    ) -> None:
        self._dispatcher = dispatcher
        self._workspace_id = workspace_id
        self._account_id = account_id
        self._model_account_id = model_account_id
        self._features = features or _SUBSTANTIAL_FEATURES
        self._projected_cost_cents = projected_cost_cents

    async def complete(
        self, *, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None
    ) -> LoopTurn:
        request = DispatchRequest(
            workspace_id=self._workspace_id,
            account_id=self._account_id,
            model_account_id=self._model_account_id,
            messages=[dict(m) for m in messages],
            features=self._features,
            projected_cost_cents=self._projected_cost_cents,
            tools=[dict(t) for t in tools] if tools else None,
        )
        result = await self._dispatcher.dispatch(request)
        response = result.response
        return LoopTurn(
            content=response.content,
            tool_calls=_to_loop_tool_calls(response.tool_calls),
        )


def _to_loop_tool_calls(raw: tuple[dict[str, Any], ...]) -> tuple[LoopToolCall, ...]:
    calls: list[LoopToolCall] = []
    for call in raw:
        function = call.get("function") or {}
        arguments = _decode_arguments(function.get("arguments"))
        calls.append(
            LoopToolCall(
                id=str(call.get("id") or ""),
                name=str(function.get("name") or ""),
                arguments=arguments,
            )
        )
    return tuple(calls)


def _decode_arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            decoded = json.loads(raw)
        except (ValueError, TypeError):
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}


__all__ = ["GatewayLoopLlm"]
