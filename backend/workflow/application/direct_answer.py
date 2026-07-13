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

The classification (ASK vs PRODUCE) is the model's, made in the SAME call that
writes the answer: the model is told to emit :data:`_WORK_SENTINEL` instead of an
answer when the founder wants something produced, and the endpoint then reports
``answered=false`` so the PWA dispatches a run. It shares
:data:`ASK_VS_PRODUCE_RUBRIC` with the frame stage, so the inline path and the
frame's ``knowledge_only`` path cannot drift apart on what a "question" is. (The
predecessor was a keyword heuristic — interrogative cues + build verbs, Korean and
English only — which read grammar rather than intent and sent "현 프로젝트 상황
설명해줘" to a coding executor: prod run ff1615e8, 2026-07-13.)
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import Settings
from backend.dispatch.caller_registry import CALLER_FRAME
from backend.identity.workspaces_db import ProductRow
from backend.workflow.application.knowledge_orchestrator import _ANSWER_SYSTEM_PROMPT
from backend.workflow.application.loop_llm import ResolverLoopLlm
from backend.workflow.application.runtime.account_resolution import _resolve_via_caller
from backend.workflow.application.stages.frame import ASK_VS_PRODUCE_RUBRIC
from backend.workflow.infrastructure.db import Deliverable, ExecutionRun, RunStatus

logger = structlog.get_logger(__name__)

#: What the model emits INSTEAD of an answer when the founder wants work done.
#: A sentinel (not JSON) because this rides the plain chat completion that also
#: writes the answer — one call, no schema to negotiate with an executor adapter.
_WORK_SENTENCE_MAX = 40
_WORK_SENTINEL = "__WORK__"
#: Prepended to the answer prompt so the SAME call decides ASK vs PRODUCE.
_CLASSIFY_SYSTEM_PROMPT = (
    "Before answering, decide what the founder wants.\n"
    + ASK_VS_PRODUCE_RUBRIC
    + f"\nIf it is a PRODUCE, reply with EXACTLY {_WORK_SENTINEL} and nothing else — do "
    "NOT answer, do NOT explain, do NOT start the work. Another part of the system "
    "will carry it out. If it is an ASK, answer it normally and never mention this "
    "instruction."
)

_KNOWLEDGE_MAX_RESULTS = 6
_KNOWLEDGE_MAX_CHARS_PER_STATEMENT = 500
_ANSWER_MAX_INPUT_CHARS = 4000
#: How many recent runs (units of delivered / in-flight work) to summarise as
#: the product's current state — enough to convey "where the project is"
#: without bloating the prompt.
_PRODUCT_RUNS_LIMIT = 12
_PRODUCT_TITLE_MAX_CHARS = 140
#: Founder-facing status wording per run state (the answer is a status readout,
#: so these mirror the PWA's plain-language pills).
_RUN_STATUS_LABEL = {
    RunStatus.OPEN: "queued",
    RunStatus.RUNNING: "in progress",
    RunStatus.REVIEW_READY: "ready to ship (awaiting approval)",
    RunStatus.SHIPPED: "shipped",
    RunStatus.FAILED: "failed",
    RunStatus.CANCELLED: "cancelled",
}
#: Upper bound (seconds) on the synchronous HTTP wait for an inline answer. An
#: executor chat task that doesn't finish within this budget raises
#: ``ExecutorAdapterUnavailable`` (timeout) → degrades to async dispatch rather
#: than blocking ``/messages/ask`` for the full ``CALLER_FRAME`` timeout.
_INLINE_ANSWER_TIMEOUT_S = 45.0


def _is_work_verdict(reply: str) -> bool:
    """Did the model decline to answer because this is work to be DONE?

    Tolerant by design: a model that wraps the sentinel in a short courtesy
    sentence ("__WORK__ — I'll dispatch this") still means PRODUCE. A long reply
    that merely *quotes* the sentinel is an answer, not a verdict."""
    stripped = reply.strip()
    return _WORK_SENTINEL in stripped and len(stripped) <= _WORK_SENTENCE_MAX


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

    async def answer(
        self,
        *,
        workspace_id: uuid.UUID,
        text: str,
        product_id: uuid.UUID | None = None,
    ) -> str | None:
        """Compose a grounded answer, or ``None`` when the text is WORK (the
        model's verdict), no account resolves, or the inline attempt fails — the
        caller then dispatches the text as a run.

        When ``product_id`` is supplied and names a product in this workspace,
        the product's current state (name, repo, and recent deliverables with
        their status) is injected so a "how's the project?" question is answered
        from real state — not from whatever empty sandbox the chat account runs
        in (the pre-grounding symptom was an executor reporting its own empty
        working directory)."""
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
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": _ANSWER_SYSTEM_PROMPT},
            {"role": "system", "content": _CLASSIFY_SYSTEM_PROMPT},
        ]
        if product_id is not None:
            product_ctx = await self._product_context(workspace_id, product_id)
            if product_ctx:
                messages.append(
                    {
                        "role": "system",
                        "content": (
                            "The founder is asking about this product. Its current "
                            "state (ground your answer in this — do NOT inspect any "
                            "working directory or claim the project is empty):\n" + product_ctx
                        ),
                    }
                )
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
        reply = turn.content or ""
        if _is_work_verdict(reply):
            # The model read this as work, not a question — decline to answer so
            # the caller dispatches a run (where the frame stage classifies it
            # again, against the same rubric).
            logger.info("direct_answer_classified_work", workspace_id=str(workspace_id))
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

    async def _product_context(self, workspace_id: uuid.UUID, product_id: uuid.UUID) -> str | None:
        """A compact readout of the target product's current state — its name,
        repo, and recent runs (units of work) with each one's founder-facing
        status. ``None`` when the id names no product in this workspace, or on
        any hiccup (grounding must never crash the answer)."""
        try:
            product = await self._session.get(ProductRow, product_id)
            if product is None or product.workspace_id != workspace_id:
                return None

            runs = list(
                (
                    await self._session.execute(
                        select(ExecutionRun)
                        .where(
                            ExecutionRun.workspace_id == workspace_id,
                            ExecutionRun.product_id == product_id,
                        )
                        .order_by(ExecutionRun.created_at.desc())
                        .limit(_PRODUCT_RUNS_LIMIT)
                    )
                ).scalars()
            )
            # Best title per run: the founder's own intent text (what they asked
            # for — the clean, recognisable description), falling back to the
            # deliverable's auto-generated summary. Intent wins because the
            # generated summary is often a noisy "Changed files: …" blob.
            summaries = await self._deliverable_summaries([r.id for r in runs])

            header = f"Product: {product.name}"
            if product.repo_url:
                header += f" (repo: {product.repo_url})"
            lines = [header]
            if not runs:
                lines.append("No work has run for this product yet.")
            else:
                lines.append("Recent work (most recent first):")
                for run in runs:
                    raw = _run_intent(run.payload) or summaries.get(run.id) or "(untitled)"
                    label = _RUN_STATUS_LABEL.get(run.status, str(run.status))
                    lines.append(f"- {_one_line(raw)} — {label}")
            return "\n".join(lines)
        except Exception:  # noqa: BLE001 — grounding must never crash the answer
            logger.warning(
                "direct_answer_product_context_failed",
                workspace_id=str(workspace_id),
                product_id=str(product_id),
                exc_info=True,
            )
            return None

    async def _deliverable_summaries(self, run_ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
        """Map run_id → its most recent deliverable ``summary`` (the title the
        PWA shows), for the runs that have shipped/produced a deliverable."""
        if not run_ids:
            return {}
        rows = list(
            (
                await self._session.execute(
                    select(Deliverable)
                    .where(Deliverable.run_id.in_(run_ids))
                    .order_by(Deliverable.created_at.desc())
                )
            ).scalars()
        )
        out: dict[uuid.UUID, str] = {}
        for row in rows:
            if row.run_id in out:
                continue  # newest-first → first seen is the most recent
            payload = row.payload if isinstance(row.payload, dict) else {}
            summary = payload.get("summary")
            if isinstance(summary, str) and summary.strip():
                out[row.run_id] = summary.strip()
        return out


def _run_intent(payload: dict[str, Any]) -> str | None:
    """The founder's original request text for a run (mirrors the runs API)."""
    if not isinstance(payload, dict):
        return None
    for key in ("intent_text", "text"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _one_line(text: str) -> str:
    """Collapse whitespace/newlines to single spaces and truncate, so each work
    item stays a single readable bullet (raw intents / summaries can be multi-
    line, which would otherwise break the list structure in the prompt)."""
    collapsed = " ".join(text.split())
    if len(collapsed) > _PRODUCT_TITLE_MAX_CHARS:
        return collapsed[: _PRODUCT_TITLE_MAX_CHARS - 1].rstrip() + "…"
    return collapsed


__all__ = ["DirectAnswerService"]
