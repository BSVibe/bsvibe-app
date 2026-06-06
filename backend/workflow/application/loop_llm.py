"""ResolverLoopLlm — production :class:`LoopLlm` for the agent loop.

Routes every plan/act/judge turn through a
:class:`~backend.dispatch.adapter.ModelAccountAdapter` the agent
runtime resolved up front from
:class:`~backend.dispatch.resolver.ModelAccountResolver`. The work loop
in :mod:`backend.execution.orchestrator` depends only on the ``LoopLlm``
Protocol, so this adapter and a deterministic test stub are
interchangeable.

After Lift E2 the classifier-driven :class:`GatewayDispatcher` is gone —
this adapter speaks directly to the resolver's adapter object.
"""

from __future__ import annotations

import json
from typing import Any

from backend.dispatch.adapter import ChatToolCall, ModelAccountAdapter
from backend.workflow.application.agent_loop import LoopToolCall, LoopTurn


class ResolverLoopLlm:
    """Adapts a :class:`ModelAccountAdapter` to the ``LoopLlm`` Protocol."""

    __slots__ = ("_adapter",)

    def __init__(self, *, adapter: ModelAccountAdapter) -> None:
        self._adapter = adapter

    async def complete(
        self, *, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None
    ) -> LoopTurn:
        # The loop's messages already include a system message; the
        # adapter contract expects ``system`` separate and ``messages``
        # without the leading system slot. Pull it off when present so
        # the wire shape is correct, otherwise pass an empty system.
        copied = [dict(m) for m in messages]
        system = ""
        if copied and copied[0].get("role") == "system":
            system = str(copied[0].get("content") or "")
            copied = copied[1:]
        response = await self._adapter.chat(
            system=system,
            messages=copied,
            tools=[dict(t) for t in tools] if tools else None,
        )
        return LoopTurn(
            content=response.content,
            tool_calls=_to_loop_tool_calls(response.tool_calls),
        )


def _to_loop_tool_calls(raw: tuple[ChatToolCall, ...]) -> tuple[LoopToolCall, ...]:
    calls: list[LoopToolCall] = []
    for call in raw:
        arguments = _decode_arguments(call.arguments_json)
        calls.append(
            LoopToolCall(
                id=call.id,
                name=call.name,
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


__all__ = ["ResolverLoopLlm"]
