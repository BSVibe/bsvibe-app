"""ReportNarrativeService — a plain-language "what this did" for the report (R1).

The redesigned deliverable report leads with a human description of WHAT the work
accomplished, not the raw changed-file list. A chat model (``CALLER_FRAME``, the
same cheap tier the frame stage uses) composes a 2-3 sentence summary from the
founder's intent + the captured diff. Generated lazily on first report view and
cached on the deliverable payload; a missing chat model yields ``None`` (the
report falls back to the request line), never a crash.
"""

from __future__ import annotations

import uuid
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import Settings
from backend.dispatch.caller_registry import CALLER_FRAME
from backend.identity.output_language import language_directive
from backend.workflow.application.loop_llm import ResolverLoopLlm
from backend.workflow.application.runtime.account_resolution import _resolve_via_caller

logger = structlog.get_logger(__name__)

_DIFF_MAX_CHARS = 6000
_SUMMARY_MAX_CHARS = 1000
_NARRATIVE_SYSTEM_PROMPT = (
    "You write a SHORT (2-3 sentences) plain-language summary of what an "
    "engineering change accomplished, for a non-developer founder to read. "
    "Describe what was built and its key behaviour in human terms. Do NOT include "
    "file paths, code identifiers in backticks, type signatures, or a list of "
    "changed files. Present tense, plain, no preamble like 'This change'. Just say "
    "what it now does."
)


class ReportNarrativeService:
    """Compose a plain-language narrative of a verified deliverable's work."""

    def __init__(self, session: AsyncSession, *, settings: Settings, redis: Any = None) -> None:
        self._session = session
        self._settings = settings
        # The frame caller may resolve an EXECUTOR account (a claude_code CLI
        # worker). Its adapter needs the dispatch redis to reach the worker
        # stream — the report endpoint threads the backend's dispatch client in,
        # exactly like the inline Direct-answer path does. ``None`` keeps a
        # LiteLLM-account workspace working (executor account then soft-fails to
        # None, and the report falls back to the request line).
        self._redis = redis

    async def _resolve_chat(self, workspace_id: uuid.UUID) -> ResolverLoopLlm | None:
        resolved = await _resolve_via_caller(
            self._session,
            caller_id=CALLER_FRAME,
            workspace_id=workspace_id,
            settings=self._settings,
            redis=self._redis,
        )
        if resolved is None:
            return None
        return ResolverLoopLlm(adapter=resolved.adapter)

    async def narrate(
        self,
        *,
        workspace_id: uuid.UUID,
        intent: str | None,
        summary: str | None,
        diff: str | None,
        language: str | None = None,
    ) -> str | None:
        """A 2-3 sentence plain-language "what this did", or ``None`` when no chat
        model resolves (best-effort: any hiccup degrades to ``None``).

        ``language`` (the workspace output language, ``ko`` / ``en``) makes the
        model write the summary in the founder's language — code / identifiers /
        paths stay verbatim. ``None`` / ``en`` adds nothing to the prompt."""
        llm = await self._resolve_chat(workspace_id)
        if llm is None:
            return None
        user_parts: list[str] = []
        if intent and intent.strip():
            user_parts.append(f"The founder asked for:\n{intent.strip()}")
        if summary and summary.strip():
            user_parts.append(f"Files touched:\n{summary.strip()[:_SUMMARY_MAX_CHARS]}")
        if diff and diff.strip():
            user_parts.append(f"The change (unified diff):\n{diff.strip()[:_DIFF_MAX_CHARS]}")
        if not user_parts:
            return None
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": _NARRATIVE_SYSTEM_PROMPT + language_directive(language)},
            {"role": "user", "content": "\n\n".join(user_parts)},
        ]
        try:
            turn = await llm.complete(messages=messages, tools=None)
        except Exception:  # noqa: BLE001 — a narrative hiccup must never break the report
            logger.warning("report_narrative_failed", workspace_id=str(workspace_id), exc_info=True)
            return None
        text = (turn.content or "").strip()
        return text or None


__all__ = ["ReportNarrativeService"]
