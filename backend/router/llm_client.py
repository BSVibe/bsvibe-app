"""LlmClient — folded ``bsvibe-llm`` wrapper.

A thin async wrapper around ``litellm`` so the rest of the gateway code
depends on one in-monorepo surface instead of importing litellm
directly (mirrors the BSGateway/BSNexus convention from the legacy
codebase). Only the chat-completion surface is exposed for Bundle 1;
streaming + embeddings can come with later bundles.

The actual ``litellm`` import is lazy because it's a heavy dep tree;
tests can pass a ``completion_fn`` callable to skip it entirely.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

CompletionFn = Callable[..., Awaitable[Any]]


@dataclass(frozen=True)
class LlmResponse:
    """The minimum useful surface the gateway needs from any provider."""

    content: str
    usage_prompt_tokens: int
    usage_completion_tokens: int
    raw: Any | None = None
    # OpenAI-shaped tool calls the model emitted, normalized to plain
    # dicts: ``{"id", "type", "function": {"name", "arguments"}}``. Empty
    # when the caller passed no ``tools`` or the model returned none. The
    # agent loop (backend.execution.orchestrator) consumes these; the
    # plain chat path ignores them.
    tool_calls: tuple[dict[str, Any], ...] = ()


def _lazy_litellm_completion() -> CompletionFn:
    import litellm  # noqa: PLC0415 — optional dep, lazy

    return litellm.acompletion  # type: ignore[no-any-return]


class LlmClient:
    def __init__(self, *, completion_fn: CompletionFn | None = None) -> None:
        self._completion = completion_fn or _lazy_litellm_completion()

    async def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        api_base: str | None = None,
        api_key: str | None = None,
        extra_params: dict[str, Any] | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> LlmResponse:
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
        }
        if api_base:
            kwargs["api_base"] = api_base
        if api_key:
            kwargs["api_key"] = api_key
        if tools:
            kwargs["tools"] = tools
        if extra_params:
            kwargs.update(extra_params)

        raw = await self._completion(**kwargs)

        # litellm-shaped response — fall back to dict access for mocks.
        choices = _attr_or_key(raw, "choices") or []
        first = choices[0] if choices else {}
        message = _attr_or_key(first, "message") or {}
        content = _attr_or_key(message, "content") or ""

        usage = _attr_or_key(raw, "usage") or {}
        prompt_tokens = int(_attr_or_key(usage, "prompt_tokens") or 0)
        completion_tokens = int(_attr_or_key(usage, "completion_tokens") or 0)

        return LlmResponse(
            content=str(content),
            usage_prompt_tokens=prompt_tokens,
            usage_completion_tokens=completion_tokens,
            raw=raw,
            tool_calls=_normalize_tool_calls(_attr_or_key(message, "tool_calls")),
        )


def _attr_or_key(obj: Any, name: str) -> Any:
    if obj is None:
        return None
    if hasattr(obj, name):
        return getattr(obj, name)
    if isinstance(obj, dict):
        return obj.get(name)
    return None


def _normalize_tool_calls(raw: Any) -> tuple[dict[str, Any], ...]:
    """Coerce litellm/OpenAI tool_calls (objects or dicts) into plain
    ``{"id", "type", "function": {"name", "arguments"}}`` dicts."""
    if not raw:
        return ()
    normalized: list[dict[str, Any]] = []
    for call in raw:
        function = _attr_or_key(call, "function") or {}
        normalized.append(
            {
                "id": str(_attr_or_key(call, "id") or ""),
                "type": str(_attr_or_key(call, "type") or "function"),
                "function": {
                    "name": str(_attr_or_key(function, "name") or ""),
                    "arguments": str(_attr_or_key(function, "arguments") or ""),
                },
            }
        )
    return tuple(normalized)
