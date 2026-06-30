"""DirectAnswerService — synchronous inline answer for a Direct *question* (L10).

A founder's Direct question (not a build request) is answered INLINE in the
Direct modal instead of being dispatched as a run. This bypasses the agent /
executor loop entirely: a chat answer needs a CHAT model (``CALLER_FRAME``),
never the coding-agent executor — the prod symptom of routing a question into
the executor was "executor chat task … failed: exit 1"
([[bsvibe-executor-subprocess-too-heavy]]).

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
from backend.router.accounts.predicates import EXECUTOR_PROVIDER
from backend.workflow.application.knowledge_orchestrator import _ANSWER_SYSTEM_PROMPT
from backend.workflow.application.loop_llm import ResolverLoopLlm
from backend.workflow.application.runtime.account_resolution import _resolve_via_caller
from backend.workflow.application.stages.frame import _is_answer_first_question

logger = structlog.get_logger(__name__)

_KNOWLEDGE_MAX_RESULTS = 6
_KNOWLEDGE_MAX_CHARS_PER_STATEMENT = 500
_ANSWER_MAX_INPUT_CHARS = 4000


def is_question(text: str) -> bool:
    """Deterministic: should this Direct text be ANSWERED inline rather than
    dispatched as work? A question with no build verb (``artifact_hint`` is
    unknown at intake, so pass ``None``) — same rule the frame uses."""
    return _is_answer_first_question(text, None)


class DirectAnswerService:
    """Answer a founder's question synchronously from workspace knowledge."""

    def __init__(self, session: AsyncSession, *, settings: Settings) -> None:
        self._session = session
        self._settings = settings

    async def answer(self, *, workspace_id: uuid.UUID, text: str) -> str | None:
        """Compose a grounded answer, or ``None`` when no chat model resolves
        for the workspace (the caller then dispatches the text as work)."""
        chat = await _resolve_via_caller(
            self._session,
            caller_id=CALLER_FRAME,
            workspace_id=workspace_id,
            settings=self._settings,
        )
        if chat is None:
            logger.info("direct_answer_no_chat_account", workspace_id=str(workspace_id))
            return None
        # An inline synchronous answer can NOT dispatch to an executor account
        # (the coding-agent loop needs a worker-stream transport this HTTP path
        # has no business spinning up) — routing a question there raised
        # ``ExecutorAdapterUnavailable`` → an unhandled 500 the browser surfaced
        # as a CORS error ("Network hiccup"). CALLER_FRAME *should* resolve a
        # chat model, but a workspace with only executor accounts falls back to
        # one; degrade to ``None`` (answered=false → async dispatch) instead.
        if getattr(chat.account, "provider", None) == EXECUTOR_PROVIDER:
            logger.info("direct_answer_executor_only_account", workspace_id=str(workspace_id))
            return None
        llm = ResolverLoopLlm(adapter=chat.adapter)
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
