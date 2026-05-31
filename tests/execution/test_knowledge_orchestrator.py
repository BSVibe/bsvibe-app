"""KnowledgeAnswerOrchestrator — the B9b knowledge-only short-circuit.

A run framed ``knowledge_only`` (a question answerable from the BSage ontology)
is answered DIRECTLY with ONE LLM call and SKIPS the plan→act→verify agent loop
(Workflow §1.2 "Frame path branch" — "one LLM call total, real cost saver").

These prove the delta:

* one LLM call, retrieves knowledge, writes an ANSWER deliverable, reaches a
  terminal (``verified`` outcome → REVIEW_READY),
* it does NOT run the agent loop / sandbox / verify (no VerificationResult, never
  marked verified-as-code: WorkStep stays UNTESTED, deliverable is DIRECT_OUTPUT),
* it never raises (a retrieval hiccup / no knowledge degrades gracefully).
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from backend.execution.db import (
    Deliverable,
    DeliverableType,
    ExecutionRun,
    ProofState,
    RunStatus,
    VerificationResult,
    WorkStep,
    WorkStepStatus,
)
from backend.execution.knowledge_orchestrator import (
    KNOWLEDGE_ANSWER_KIND,
    KnowledgeAnswerOrchestrator,
)
from backend.workflow.application.agent_loop import LoopTurn
from backend.workflow.infrastructure.delivery.db import (
    DeliveryEventRow,  # noqa: F401 — register table for memory_session
)
from tests._support import memory_session

pytestmark = pytest.mark.asyncio


class _CountingLlm:
    """A :class:`LoopLlm` that records every (messages, tools) and returns a
    fixed answer. ``tools`` MUST always be ``None`` for the knowledge path —
    there is no tool loop."""

    def __init__(self, answer: str = "The answer is 42.") -> None:
        self._answer = answer
        self.calls: list[dict[str, Any]] = []

    async def complete(
        self, *, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None
    ) -> LoopTurn:
        self.calls.append({"messages": list(messages), "tools": tools})
        return LoopTurn(content=self._answer, tool_calls=())


class _StubRetriever:
    """A :class:`CanonRetriever` returning fixed canonical patterns."""

    def __init__(self, patterns: list[str]) -> None:
        self._patterns = patterns
        self.queried: list[str] = []

    async def retrieve_for_signals(self, signals: str) -> list[str]:
        self.queried.append(signals)
        return list(self._patterns)


class _ExplodingRetriever:
    """A retriever that raises — proves graceful degradation (never crash)."""

    async def retrieve_for_signals(self, signals: str) -> list[str]:
        raise RuntimeError("vault unavailable")


async def _seed_run(session: Any, *, intent: str, framed_intent: str | None = None) -> ExecutionRun:
    run = ExecutionRun(
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        product_id=None,
        request_id=None,
        status=RunStatus.RUNNING,
        payload={
            "intent_text": intent,
            "frame": {
                "skill_match": None,
                "artifact_type_hint": None,
                "framed_intent": framed_intent,
                "path_classification": "knowledge_only",
            },
        },
    )
    session.add(run)
    await session.flush()
    return run


async def test_knowledge_answer_one_llm_call_writes_answer_deliverable(tmp_path: Path) -> None:
    """The cost-saver delta: ONE LLM call, retrieves knowledge, writes an ANSWER
    deliverable, reaches a terminal — and NO agent loop / sandbox / verify."""
    async with memory_session() as session:
        run = await _seed_run(
            session, intent="What is our deployment policy?", framed_intent="Explain deploy policy"
        )
        llm = _CountingLlm(answer="Deploy via the gateway; never raw litellm.")
        retriever = _StubRetriever(["Always wrap bsvibe_llm.LlmClient."])
        orch = KnowledgeAnswerOrchestrator(session=session, llm=llm, retriever=retriever)

        result = await orch.run(run=run, workspace_dir=tmp_path)

        # Terminal outcome → AgentRunner maps it to REVIEW_READY.
        assert result.outcome == "verified"
        # EXACTLY ONE LLM call (the cost saver) — no plan/act/verify turns.
        assert len(llm.calls) == 1
        # The one call is a plain completion (no tools — no agent tool loop).
        assert llm.calls[0]["tools"] is None
        # Knowledge was retrieved + grounded into the prompt.
        assert retriever.queried, "retriever must be consulted"
        blob = "\n".join(
            m.get("content", "")
            for m in llm.calls[0]["messages"]
            if isinstance(m.get("content"), str)
        )
        assert "Always wrap bsvibe_llm.LlmClient." in blob

        # An ANSWER deliverable was written — honestly (NOT code, NOT verified).
        deliverable = (await session.execute(select(Deliverable))).scalar_one()
        assert deliverable.deliverable_type is DeliverableType.DIRECT_OUTPUT
        assert deliverable.deliverable_type is not DeliverableType.CODE
        assert deliverable.payload.get("kind") == KNOWLEDGE_ANSWER_KIND
        assert deliverable.payload.get("answer") == "Deploy via the gateway; never raw litellm."


async def test_knowledge_answer_is_not_verified_code(tmp_path: Path) -> None:
    """Trust integrity (B4): the answer is NOT a green verified-code deliverable.

    No VerificationResult is created, the WorkStep is never PROVED/VERIFIED, and
    the deliverable type is not CODE."""
    async with memory_session() as session:
        run = await _seed_run(session, intent="Summarise the auth model")
        llm = _CountingLlm()
        orch = KnowledgeAnswerOrchestrator(session=session, llm=llm, retriever=_StubRetriever([]))

        await orch.run(run=run, workspace_dir=tmp_path)

        # NO VerificationResult — the loop's verify path never ran.
        assert (await session.execute(select(VerificationResult))).first() is None

        # The WorkStep is honest: never PROVED, never the VERIFIED status that
        # the code path sets on a passing contract.
        work_step = (await session.execute(select(WorkStep))).scalar_one()
        assert work_step.proof_state is not ProofState.PROVED
        assert work_step.proof_state is ProofState.UNTESTED
        assert work_step.status is not WorkStepStatus.VERIFIED

        # The deliverable is NOT code.
        deliverable = (await session.execute(select(Deliverable))).scalar_one()
        assert deliverable.deliverable_type is not DeliverableType.CODE


async def test_knowledge_answer_no_retriever_still_answers(tmp_path: Path) -> None:
    """No retriever → still one LLM call + an answer (knowledge optional)."""
    async with memory_session() as session:
        run = await _seed_run(session, intent="Quick question")
        llm = _CountingLlm()
        orch = KnowledgeAnswerOrchestrator(session=session, llm=llm, retriever=None)

        result = await orch.run(run=run, workspace_dir=tmp_path)

        assert result.outcome == "verified"
        assert len(llm.calls) == 1
        assert (await session.execute(select(Deliverable))).scalar_one() is not None


async def test_knowledge_answer_retrieval_hiccup_degrades_gracefully(tmp_path: Path) -> None:
    """A retrieval failure NEVER strands the run — it answers without knowledge."""
    async with memory_session() as session:
        run = await _seed_run(session, intent="Anything")
        llm = _CountingLlm()
        orch = KnowledgeAnswerOrchestrator(
            session=session, llm=llm, retriever=_ExplodingRetriever()
        )

        result = await orch.run(run=run, workspace_dir=tmp_path)

        assert result.outcome == "verified"
        assert len(llm.calls) == 1
        assert (await session.execute(select(Deliverable))).scalar_one() is not None
