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


def _lazy_litellm_completion() -> CompletionFn:
    import litellm  # type: ignore[import-not-found]  # noqa: PLC0415 — optional dep, lazy

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
    ) -> LlmResponse:
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
        }
        if api_base:
            kwargs["api_base"] = api_base
        if api_key:
            kwargs["api_key"] = api_key
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
        )


def _attr_or_key(obj: Any, name: str) -> Any:
    if obj is None:
        return None
    if hasattr(obj, name):
        return getattr(obj, name)
    if isinstance(obj, dict):
        return obj.get(name)
    return None
