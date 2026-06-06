"""Resolver-backed LLM seams for the workflow runtime (Lift E2).

After Lift E2 the classifier-driven :class:`GatewayDispatcher` is gone;
this module hosts the two thin adapters the runtime needs that translate
between workflow-specific call shapes (``CompileLlm`` /
``FrameLlm.complete_text``) and the dispatch resolver's adapter
``chat(...)`` verb:

* :class:`_ResolverCompileLlm` — adapts the resolver's adapter to
  BSage's :class:`CompileLlm` seam (settle extractor / product
  bootstrap / knowledge ingest — a single chat call per chunk).
* :class:`_ResolverFrameLlm` — adapts the resolver's adapter to the
  :class:`FrameLlm.complete_text` seam (the frame stage's cheap completion).

Both are constructed by the runtime factories
(``settle_runtime``, ``product_bootstrap_runtime``,
``agent_runtime``) once per workspace via
:class:`backend.dispatch.resolver.ModelAccountResolver`.

The legacy ``build_gateway_dispatcher`` is gone — no caller constructs
it any more. ``_GatewayCompileLlm`` / ``_GatewayFrameLlm`` are removed
because they carried hardcoded ``ClassificationFeatures`` (the very
heuristics this lift deletes).
"""

from __future__ import annotations

from typing import Any

import structlog

from backend.dispatch.adapter import ModelAccountAdapter

logger = structlog.get_logger(__name__)


class _ResolverCompileLlm:
    """Thin :class:`CompileLlm` adapter over a resolved adapter.

    The compile path asks for ONE structured plan per chunk; the adapter
    speaks OpenAI-shape messages, so we map the system+messages tuple
    onto :meth:`ModelAccountAdapter.chat` and return the response text.
    Tool calls aren't expected on this path (no ``tools`` kwarg) and are
    ignored if the model emits them.
    """

    __slots__ = ("_adapter",)

    def __init__(self, *, adapter: ModelAccountAdapter) -> None:
        self._adapter = adapter

    async def chat(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        suppress_reasoning: bool = False,
        timeout_s: float | None = None,
    ) -> str:
        del suppress_reasoning, timeout_s  # ignored — the seam does not own them
        response = await self._adapter.chat(
            system=system,
            messages=[dict(m) for m in messages],
            tools=None,
        )
        return str(response.content)


class _ResolverFrameLlm:
    """Thin :class:`FrameLlm.complete_text` adapter over a resolved adapter.

    Framing is a single ``(system, user)`` → text call. We collapse it
    onto :meth:`ModelAccountAdapter.chat` with a one-message conversation.
    """

    __slots__ = ("_adapter",)

    def __init__(self, *, adapter: ModelAccountAdapter) -> None:
        self._adapter = adapter

    async def complete_text(self, *, system: str, user: str) -> str:
        response = await self._adapter.chat(
            system=system,
            messages=[{"role": "user", "content": user}],
            tools=None,
        )
        return str(response.content)


__all__ = [
    "_ResolverCompileLlm",
    "_ResolverFrameLlm",
]
