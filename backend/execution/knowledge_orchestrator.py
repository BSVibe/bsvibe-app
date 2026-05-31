"""KnowledgeAnswerOrchestrator — the B9b knowledge-only short-circuit.

Workflow §1.2 "Frame path branch". B9a made the frame stage classify a path
(``knowledge_only`` | ``agent_loop``) and RECORD it on
``run.payload["frame"]["path_classification"]``. B9b is the branch that ACTS on
``knowledge_only``: a question answerable from the workspace's BSage ontology is
answered DIRECTLY with ONE cheap LLM call and SKIPS the plan→act→verify agent
loop entirely — "one LLM call total, real cost saver".

It satisfies the :class:`~backend.execution.orchestrator.RunCompute` Protocol
(``run(*, run, workspace_dir) -> LoopResult``), so the worker-runtime factory can
return it wherever it returns the native :class:`RunOrchestrator` /
:class:`~backend.executors.orchestrator.ExecutorOrchestrator`, and
:class:`~backend.workflow.application.agent_runner.AgentRunner.drive` maps its outcome
identically — ``verified → REVIEW_READY``.

HONESTY (B4 trust integrity). A knowledge answer is NOT verified code. So this
orchestrator:

* makes NO sandbox acquisition, runs NO command/judge verification, writes NO
  :class:`~backend.execution.db.VerificationResult`,
* never sets :class:`~backend.execution.db.ProofState.PROVED` and never flips the
  WorkStep to ``VERIFIED`` (it lands ``UNTESTED`` — the work was not proven),
* writes a :data:`~backend.execution.db.DeliverableType.DIRECT_OUTPUT` ANSWER
  Deliverable (NOT ``CODE``) via :func:`write_answer_deliverable`, so a consumer
  renders an answer the founder reads, never a green "verified" code change.

GRACEFUL. A retrieval hiccup degrades to answering WITHOUT knowledge (never
raises into the run); any unexpected crash maps to ``system_error`` — the run is
never stranded. The single LLM call is the only required step.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from backend.execution.db import (
    ExecutionRun,
    ProofState,
    RunAttempt,
    RunAttemptPhase,
    WorkStep,
    WorkStepStatus,
)
from backend.execution.orchestrator import CanonRetriever, LoopLlm, LoopResult
from backend.execution.verified_deliverable import (
    ANSWER_DELIVERABLE_KIND,
    write_answer_deliverable,
)

logger = structlog.get_logger(__name__)

#: Re-exported so callers/tests assert the deliverable's honest representation
#: without reaching into the write helper.
KNOWLEDGE_ANSWER_KIND = ANSWER_DELIVERABLE_KIND

# Cap the knowledge folded into the single answer prompt — top-N statements, each
# clamped, so the grounding never blows the (local-model) generation budget
# (mirrors the native B6 knowledge seed: 5 × 500).
_KNOWLEDGE_MAX_RESULTS = 5
_KNOWLEDGE_MAX_CHARS_PER_STATEMENT = 500
_INTENT_MAX_CHARS = 4_000

_ANSWER_SYSTEM_PROMPT = (
    "You are answering a founder's question directly from this workspace's "
    "established knowledge — no engineering work is required. Give a concise, "
    "accurate answer grounded in the provided knowledge. If the knowledge does "
    "not cover the question, answer from general understanding and say so plainly. "
    "Do NOT claim to have changed any code or verified anything — this is an "
    "answer, not a code change."
)


class KnowledgeAnswerOrchestrator:
    """Answer a knowledge-only ask in ONE LLM call — no agent loop (RunCompute)."""

    def __init__(
        self,
        *,
        session: AsyncSession,
        llm: LoopLlm,
        retriever: CanonRetriever | None = None,
    ) -> None:
        self._session = session
        self._llm = llm
        self._retriever = retriever

    async def run(self, *, run: ExecutionRun, workspace_dir: Path) -> LoopResult:
        """Compose + land a knowledge answer. ``workspace_dir`` is unused (no
        sandbox, no file work) — accepted to satisfy the RunCompute signature."""
        work_step = WorkStep(
            id=uuid.uuid4(),
            run_id=run.id,
            workspace_id=run.workspace_id,
            title=_intent(run, max_chars=512),
            status=WorkStepStatus.RUNNING,
            proof_state=ProofState.UNTESTED,
            payload={"kind": ANSWER_DELIVERABLE_KIND},
        )
        attempt = RunAttempt(
            id=uuid.uuid4(),
            run_id=run.id,
            workspace_id=run.workspace_id,
            phase=RunAttemptPhase.WORKING,
            payload={},
        )
        self._session.add_all([work_step, attempt])
        await self._session.flush()

        try:
            statements = await self._retrieve_knowledge(run)
            answer = await self._compose_answer(run, statements)
        except Exception as exc:  # noqa: BLE001 — any crash → system_error, never leak
            work_step.status = WorkStepStatus.FAILED
            attempt.phase = RunAttemptPhase.FAILED
            attempt.finished_at = _utcnow()
            await self._session.flush()
            logger.exception("knowledge_orchestrator_crash", run_id=str(run.id))
            return LoopResult(
                outcome="system_error",
                run_id=run.id,
                work_step_id=work_step.id,
                run_attempt_id=attempt.id,
                summary=f"knowledge answer failed: {exc}",
            )

        # HONEST terminal — an answer the founder reads, NOT verified code. The
        # WorkStep stays UNTESTED (proof_state never PROVED); the deliverable is
        # DIRECT_OUTPUT, never CODE. No VerificationResult is ever written.
        attempt.phase = RunAttemptPhase.COMPLETED
        attempt.finished_at = _utcnow()
        deliverable = await write_answer_deliverable(
            self._session,
            run,
            attempt_id=attempt.id,
            answer=answer,
            knowledge_refs=statements,
        )
        await self._session.flush()
        logger.info(
            "knowledge_orchestrator_answered",
            run_id=str(run.id),
            deliverable_id=str(deliverable.id),
            knowledge_count=len(statements),
        )
        return LoopResult(
            outcome="verified",
            run_id=run.id,
            work_step_id=work_step.id,
            run_attempt_id=attempt.id,
            written_paths=[],
            summary=answer,
        )

    async def _retrieve_knowledge(self, run: ExecutionRun) -> list[str]:
        """Retrieve canon relevant to the run's intent for grounding.

        Uses the SAME signal + retriever as the native B6 knowledge seed
        (``retrieve_for_signals(intent)``). No retriever → ``[]``; a retrieval
        hiccup degrades to ``[]`` (never raises — the answer just goes
        ungrounded, exactly the graceful-empty contract the native paths follow)."""
        if self._retriever is None:
            return []
        try:
            statements = await self._retriever.retrieve_for_signals(_intent(run))
        except Exception:  # noqa: BLE001 — grounding must never crash the answer
            logger.warning(
                "knowledge_orchestrator_retrieve_failed", run_id=str(run.id), exc_info=True
            )
            return []
        cleaned = [
            s.strip()[:_KNOWLEDGE_MAX_CHARS_PER_STATEMENT] for s in statements if s and s.strip()
        ][:_KNOWLEDGE_MAX_RESULTS]
        return cleaned

    async def _compose_answer(self, run: ExecutionRun, statements: list[str]) -> str:
        """The ONE LLM call — a plain completion (``tools=None``: no tool loop).

        The question is the run's stable intent; the retrieved knowledge is folded
        in as grounding context. Returns the model's answer text."""
        user = _intent(run, max_chars=_INTENT_MAX_CHARS)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": _ANSWER_SYSTEM_PROMPT},
        ]
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
        messages.append({"role": "user", "content": user})
        turn = await self._llm.complete(messages=messages, tools=None)
        return turn.content


def _intent(run: ExecutionRun, *, max_chars: int = _INTENT_MAX_CHARS) -> str:
    """The run's stable question text — the framed intent when present, else the
    raw intent (mirrors the native ``_intent_title``). Never the LLM's free
    output, never written_paths."""
    payload = run.payload or {}
    frame = payload.get("frame") if isinstance(payload, dict) else None
    framed_intent = frame.get("framed_intent") if isinstance(frame, dict) else None
    if isinstance(framed_intent, str) and framed_intent.strip():
        return framed_intent.strip()[:max_chars]
    text = payload.get("intent_text") or payload.get("text") or "Untitled run"
    return str(text)[:max_chars]


def _utcnow() -> Any:
    from datetime import UTC, datetime  # noqa: PLC0415 — local to avoid top-level churn

    return datetime.now(tz=UTC)


__all__ = ["KNOWLEDGE_ANSWER_KIND", "KnowledgeAnswerOrchestrator"]
