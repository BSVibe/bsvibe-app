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

#930 — both adapters used to end in ``return str(response.content)``. The
token usage was ON that same response object and died on that line, so every
call through this seam was spend no meter anywhere could see. The two adapters
now differ because their CONSUMERS differ, not because the fix is half-applied:

* :class:`_ResolverFrameLlm` RETURNS the usage
  (:class:`~backend.workflow.application.stages.frame.TextCompletion`). Its
  primary consumer is the frame stage, whose caller holds the run the framing
  belongs to — there is a real home, so the number travels to it.
* :class:`_ResolverCompileLlm` keeps its ``-> str`` contract and LOGS the usage
  as :data:`UNATTRIBUTED_USAGE_EVENT`. Not one of its three consumers (knowledge
  ingest, product bootstrap, the settle extractor) has an ``execution_runs`` row
  to accrue onto — product bootstrap's ``bootstrap_run_id`` is a loose
  correlation uuid with no FK, not a run. Changing ``CompileLlm`` (which lives
  in the knowledge context) to carry a number every consumer would immediately
  drop would be a contract change bought for nothing; a named event makes the
  remaining gap MEASURABLE instead of invisible, which is the actual ask.
"""

from __future__ import annotations

import uuid
from typing import Any

import structlog

from backend.dispatch.adapter import ModelAccountAdapter
from backend.workflow.application.stages.frame import TextCompletion

logger = structlog.get_logger(__name__)


#: The one event name every LLM call with no run to accrue to reports under.
#: Grep-able and consistent on purpose: the size of the un-metered remainder
#: has to be countable, or "we closed the leak" is an unfalsifiable claim.
UNATTRIBUTED_USAGE_EVENT = "llm_usage_unattributed"


def log_unattributed_usage(
    *,
    site: str,
    workspace_id: uuid.UUID | str,
    usage_prompt_tokens: int,
    usage_completion_tokens: int,
    **identity: str | None,
) -> None:
    """Record an LLM turn that no run can be billed for.

    ``site`` names the call path (a dispatch caller_id where there is one);
    ``identity`` carries whatever id that path DOES have (product, concept)
    so the spend can be attributed later without re-deriving the call graph.
    """
    if not usage_prompt_tokens and not usage_completion_tokens:
        return
    logger.info(
        UNATTRIBUTED_USAGE_EVENT,
        site=site,
        workspace_id=str(workspace_id),
        usage_prompt_tokens=usage_prompt_tokens,
        usage_completion_tokens=usage_completion_tokens,
        **{k: v for k, v in identity.items() if v is not None},
    )


class _ResolverCompileLlm:
    """Thin :class:`CompileLlm` adapter over a resolved adapter.

    The compile path asks for ONE structured plan per chunk; the adapter
    speaks OpenAI-shape messages, so we map the system+messages tuple
    onto :meth:`ModelAccountAdapter.chat` and return the response text.
    Tool calls aren't expected on this path (no ``tools`` kwarg) and are
    ignored if the model emits them.

    ``workspace_id`` + ``site`` exist only so the turn's usage can be reported
    under :data:`UNATTRIBUTED_USAGE_EVENT` — see this module's docstring for
    why this seam logs rather than returns.
    """

    __slots__ = ("_adapter", "_site", "_workspace_id")

    def __init__(
        self,
        *,
        adapter: ModelAccountAdapter,
        workspace_id: uuid.UUID | str,
        site: str,
    ) -> None:
        self._adapter = adapter
        self._workspace_id = workspace_id
        self._site = site

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
        log_unattributed_usage(
            site=self._site,
            workspace_id=self._workspace_id,
            usage_prompt_tokens=getattr(response, "usage_prompt_tokens", 0),
            usage_completion_tokens=getattr(response, "usage_completion_tokens", 0),
        )
        return str(response.content)


class _ResolverFrameLlm:
    """Thin :class:`FrameLlm.complete_text` adapter over a resolved adapter.

    Framing is a single ``(system, user)`` → text call. We collapse it
    onto :meth:`ModelAccountAdapter.chat` with a one-message conversation
    and hand back the text WITH the turn's usage — the frame stage's caller
    owns the run that spend belongs to (#930).
    """

    __slots__ = ("_adapter",)

    def __init__(self, *, adapter: ModelAccountAdapter) -> None:
        self._adapter = adapter

    async def complete_text(self, *, system: str, user: str) -> TextCompletion:
        response = await self._adapter.chat(
            system=system,
            messages=[{"role": "user", "content": user}],
            tools=None,
        )
        return TextCompletion(
            text=str(response.content),
            # Telemetry, not core — same tolerance ``loop_llm`` applies: a
            # response (or a test double) without usage fields reads as 0
            # rather than breaking the framing call.
            usage_prompt_tokens=getattr(response, "usage_prompt_tokens", 0),
            usage_completion_tokens=getattr(response, "usage_completion_tokens", 0),
        )


__all__ = [
    "UNATTRIBUTED_USAGE_EVENT",
    "_ResolverCompileLlm",
    "_ResolverFrameLlm",
    "log_unattributed_usage",
]
