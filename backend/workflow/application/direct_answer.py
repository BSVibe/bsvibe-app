"""DirectAnswerService — synchronous inline answer for a Direct *question* (L10).

A founder's Direct question (not a build request) is answered INLINE in the
Direct modal instead of being dispatched as a run. The answer goes to whichever
account the workspace routed for ``CALLER_FRAME`` — executor OR LiteLLM,
treated identically: the two adapters share the same ``chat()`` interface
(``is_executor_account`` is the only branch; the executor differs solely in
subscription-cost billing, not function). An earlier version special-cased
executor accounts away (degrade-to-None) on the theory that "inline can't spin
up a worker transport"; that contradicted the functional-parity design (and the
real prod symptom — "executor chat task … failed: exit 1" — was a host-side
executor auth failure, not an inline-dispatch limitation), so the executor is
now dispatched inline like any other account.

Two invariants keep the inline path safe:
* the endpoint NEVER 500s — any failure (executor at capacity / timeout / LLM
  error) is swallowed → ``None`` (answered=false → the PWA dispatches the text
  as async work). A 500 here bypasses CORS middleware and the browser reads it
  as a network error ("Network hiccup").
* the synchronous HTTP wait on an executor task is BOUNDED by
  :data:`_INLINE_ANSWER_TIMEOUT_S` so a slow / busy executor degrades to async
  instead of holding the request open for the full frame timeout (~5 min).

The classification (question vs work) reuses the SAME deterministic heuristic
the frame stage uses for answer-first routing, so the inline path and the
frame's ``knowledge_only`` path agree on what a "question" is.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import Settings
from backend.dispatch.caller_registry import CALLER_FRAME
from backend.workflow.application.knowledge_orchestrator import _ANSWER_SYSTEM_PROMPT
from backend.workflow.application.loop_llm import ResolverLoopLlm
from backend.workflow.application.runtime.account_resolution import _resolve_via_caller
from backend.workflow.application.stages.frame import _is_answer_first_question

logger = structlog.get_logger(__name__)

_KNOWLEDGE_MAX_RESULTS = 6
_KNOWLEDGE_MAX_CHARS_PER_STATEMENT = 500
_ANSWER_MAX_INPUT_CHARS = 4000
#: Upper bound (seconds) on the synchronous HTTP wait for an inline answer. An
#: executor chat task that doesn't finish within this budget raises
#: ``ExecutorAdapterUnavailable`` (timeout) → degrades to async dispatch rather
#: than blocking ``/messages/ask`` for the full ``CALLER_FRAME`` timeout.
_INLINE_ANSWER_TIMEOUT_S = 45.0


def is_question(text: str) -> bool:
    """Deterministic: should this Direct text be ANSWERED inline rather than
    dispatched as work? A question with no build verb (``artifact_hint`` is
    unknown at intake, so pass ``None``) — same rule the frame uses."""
    return _is_answer_first_question(text, None)


class DirectAnswerService:
    """Answer a founder's question synchronously from workspace knowledge."""

    def __init__(self, session: AsyncSession, *, settings: Settings, redis: Any = None) -> None:
        self._session = session
        self._settings = settings
        # Threaded to the resolver so an executor account has a worker-stream
        # transport for inline dispatch. ``None`` (no redis configured) is fine —
        # an executor adapter then raises ``ExecutorAdapterUnavailable`` and the
        # answer degrades to async, same as any other inline failure.
        self._redis = redis

    async def answer(self, *, workspace_id: uuid.UUID, text: str) -> str | None:
        """Compose a grounded answer, or ``None`` when no account resolves /
        the inline attempt fails (the caller then dispatches the text as work)."""
        chat = await _resolve_via_caller(
            self._session,
            caller_id=CALLER_FRAME,
            workspace_id=workspace_id,
            settings=self._settings,
            redis=self._redis,
        )
        if chat is None:
            logger.info("direct_answer_no_chat_account", workspace_id=str(workspace_id))
            return None
        # Executor and LiteLLM accounts are dispatched identically (functional
        # parity). Bound the synchronous HTTP wait so an executor task that does
        # not finish within the inline budget degrades to async dispatch instead
        # of blocking the request for the full CALLER_FRAME (~5 min) timeout.
        adapter = chat.adapter
        try:
            adapter.timeout_s = _INLINE_ANSWER_TIMEOUT_S
        except AttributeError:  # pragma: no cover — adapter without a timeout knob
            pass
        llm = ResolverLoopLlm(adapter=adapter)
        statements = await self._retrieve(workspace_id, text)
        messages: list[dict[str, Any]] = [{"role": "system", "content": _ANSWER_SYSTEM_PROMPT}]
        if statements:
            body = "\n".join(f"- {s}" for s in statements)
            messages.append(
                {
                    "role": "system",
                    "content": (
                        "Relevant established knowledge for this workspace "
                        "(ground your answer in this):\n" + body
                    ),
                }
            )
        messages.append({"role": "user", "content": text[:_ANSWER_MAX_INPUT_CHARS]})
        # Any LLM failure on the inline path degrades to ``None`` (answered=false
        # → the PWA dispatches the text as async work) — the endpoint must NEVER
        # 500 (a 500 here bypasses CORS middleware and reads as a network error).
        try:
            turn = await llm.complete(messages=messages, tools=None)
        except Exception:  # noqa: BLE001 — inline answer must never crash the request
            logger.warning(
                "direct_answer_llm_failed", workspace_id=str(workspace_id), exc_info=True
            )
            return None
        return turn.content

    async def _retrieve(self, workspace_id: uuid.UUID, text: str) -> list[str]:
        """Workspace canon relevant to the question — graceful-empty on any
        hiccup (an ungrounded answer beats a crash; mirrors the native path)."""
        try:
            from backend.knowledge.factory import KnowledgeFactory  # noqa: PLC0415

            retriever = KnowledgeFactory(
                region=self._settings.knowledge_default_region,
                workspace_id=str(workspace_id),
                vault_root=Path(self._settings.knowledge_vault_root),
            ).retriever()
            statements = await retriever.retrieve_for_signals(text)
        except Exception:  # noqa: BLE001 — grounding must never crash the answer
            logger.warning(
                "direct_answer_retrieve_failed", workspace_id=str(workspace_id), exc_info=True
            )
            return []
        return [
            s.strip()[:_KNOWLEDGE_MAX_CHARS_PER_STATEMENT] for s in statements if s and s.strip()
        ][:_KNOWLEDGE_MAX_RESULTS]


__all__ = ["DirectAnswerService", "is_question"]
