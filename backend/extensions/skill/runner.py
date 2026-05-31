"""invoke_skill — system-prompt inject + retrieval prime + LLM call.

Wires into the execution-layer tool registry (Bundle X) as a callable
named ``invoke_skill``. The runner itself owns no tool registry — it
adapts a ``SkillMeta`` into a single LLM call with optional ``allowed_tools``
gating, and returns the LLM's response text.

Per Workflow §6 #5 invocation flow:

1. ``SkillLoader.get(name)`` → ``SkillMeta``
2. Prime context via the knowledge retrieval API (caller injects a Protocol
   that satisfies ``Searcher``)
3. Compose system prompt = skill body + retrieved context
4. Filter caller-supplied tool list by ``meta.allowed_tools`` if set
5. Call LLM via ``completion_fn`` (caller injects — typically
   ``backend.router.llm_client``)
6. Return ``SkillRunResult``
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol

import structlog

from backend.extensions.skill.exceptions import SkillRunError
from backend.extensions.skill.loader import SkillLoader
from backend.extensions.skill.meta import SkillMeta

logger = structlog.get_logger(__name__)


class Searcher(Protocol):
    """Structural interface for the knowledge retrieval API."""

    async def search(
        self,
        query: str,
        *,
        top_k: int = 20,
        max_chars: int = 50_000,
    ) -> str: ...


CompletionFn = Callable[..., Awaitable[str]]
"""``async (system_prompt: str, user_input: str, *, model: str | None,
allowed_tools: list[str]) -> str`` — caller adapts.

The runner stays provider-agnostic; ``backend.router.llm_client`` is the
intended concrete implementation but tests pass a fake.
"""


@dataclass
class SkillRunResult:
    """Outcome of a single ``invoke_skill`` call."""

    skill_name: str
    response: str
    used_tools: list[str] = field(default_factory=list)
    retrieval_chars: int = 0


async def invoke_skill(
    *,
    name: str,
    user_input: str,
    loader: SkillLoader,
    completion_fn: CompletionFn,
    searcher: Searcher | None = None,
    available_tools: list[str] | None = None,
    retrieval_query: str | None = None,
    retrieval_top_k: int = 20,
    retrieval_max_chars: int = 50_000,
) -> SkillRunResult:
    """Invoke ``name`` against ``user_input``. Caller provides infra.

    Args:
        name: Skill identifier (must be in ``loader.registry``).
        user_input: The user/agent message to feed the skill's LLM.
        loader: Workspace-scoped ``SkillLoader``.
        completion_fn: Async LLM call (see :data:`CompletionFn`).
        searcher: Optional retrieval Protocol — when set, primes the system
            prompt with the search result.
        available_tools: Caller's full tool-name set; intersected with
            ``meta.allowed_tools`` to produce the gated set passed to the LLM.
        retrieval_query: Optional override; defaults to ``user_input``.
        retrieval_top_k / retrieval_max_chars: Retrieval budget.

    Returns:
        ``SkillRunResult`` with the LLM's text response.

    Raises:
        SkillLoadError: ``name`` not in registry.
        SkillRunError: LLM call failed.
    """
    meta: SkillMeta = loader.get(name)

    retrieval_context = ""
    if searcher is not None:
        query = retrieval_query or user_input
        retrieval_context = await searcher.search(
            query, top_k=retrieval_top_k, max_chars=retrieval_max_chars
        )

    system_prompt = _compose_system_prompt(meta, retrieval_context)

    if meta.allowed_tools:
        gated_tools = (
            [t for t in (available_tools or []) if t in meta.allowed_tools]
            if available_tools is not None
            else list(meta.allowed_tools)
        )
    else:
        gated_tools = list(available_tools or [])

    try:
        response = await completion_fn(
            system_prompt=system_prompt,
            user_input=user_input,
            model=meta.model,
            allowed_tools=gated_tools,
        )
    except Exception as exc:  # noqa: BLE001 — adapt to skill domain error
        raise SkillRunError(f"Skill '{meta.name}' invocation failed: {exc}") from exc

    return SkillRunResult(
        skill_name=meta.name,
        response=response,
        used_tools=gated_tools,
        retrieval_chars=len(retrieval_context),
    )


def _compose_system_prompt(meta: SkillMeta, retrieval_context: str) -> str:
    """Skill body + appended retrieval context (when present)."""
    parts: list[str] = []
    if meta.system_prompt:
        parts.append(meta.system_prompt)
    if retrieval_context:
        parts.append("---\n## Retrieved context\n\n" + retrieval_context)
    return "\n\n".join(parts).strip()
