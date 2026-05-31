"""B13 — Cross-run decision reuse delta (B6 work-time seed + B3 verify-time fold).

The audit's RC-5 calls out one specific hollow path: tests that "the
retriever returns the resolved decision" without proving that this content
actually FLOWS INTO the loop the agent runs. The existing
``test_decision_cross_run_reuse_e2e.py`` covers the retriever's surface; this
file goes one step further and proves the content is consumed in BOTH seams
the lift wired:

* **B6 seed**  — the resolved-decision text appears in the LLM messages on
  the SECOND run's FIRST turn (the work LLM sees the established knowledge
  BEFORE it acts).
* **B3 fold** — the resolved-decision text appears in the persisted
  ``VerificationResult.contract`` JSON as a judge criterion on the SECOND
  run (the verifier folds canon into the verify gate).

The first run resolves the decision via the API (the SAME surface the
founder uses); the SettleWorker drains the activity into the vault note. A
fresh :class:`KnowledgeFactory` retriever for the same workspace + region
is then injected into a NEW :class:`RunOrchestrator` for a second run with
overlapping intent signals. The scripted LLM declares a judge contract that
passes; the test then asserts the delta on both seams.

This complements (does not replace) the existing
``test_decision_cross_run_reuse_e2e.py`` — that one pins the retriever
contract; this one pins the consumer side.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# Side-effect imports — register cross-domain tables on the shared
# ``Base.metadata`` so ``db_engine`` materialises them in the SQLite path.
import backend.workflow.infrastructure.delivery.db  # noqa: F401
from backend.api.deps import (
    get_current_user,
    get_current_user_row,
    get_db_session,
    get_workspace_id,
)
from backend.api.main import create_app
from backend.execution.db import (
    Decision,
    DecisionStatus,
    Deliverable,
    ExecutionRun,
    ProofState,
    RunStatus,
    VerificationOutcome,
    VerificationResult,
    WorkStep,
    WorkStepStatus,
)
from backend.execution.orchestrator import (
    LoopToolCall,
    LoopTurn,
    RunOrchestrator,
)
from backend.knowledge.factory import KnowledgeFactory
from backend.supervisor.sandbox import NoopSandboxManager
from backend.workers.settle_worker import (
    KnowledgeSettleSink,
    SettleWorker,
    SettleWorkerConfig,
)
from tests._support import db_engine, fake_current_user

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------
# Local test doubles — minimal, deliberately not shared with other modules.
# A loop-level :class:`ScriptedLlm` already lives in
# ``tests/execution/test_run_orchestrator.py``; duplicating a small one here
# keeps this file's anti-regression behaviour self-contained.
# --------------------------------------------------------------------------


class _ScriptedLlm:
    """A deterministic :class:`LoopLlm`. Records every (messages, tools) call."""

    def __init__(self, turns: list[LoopTurn]) -> None:
        self._turns = list(turns)
        self.calls: list[dict[str, Any]] = []

    async def complete(
        self, *, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None
    ) -> LoopTurn:
        self.calls.append({"messages": list(messages), "tools": tools})
        if not self._turns:
            raise AssertionError("scripted LLM exhausted — unexpected extra turn")
        return self._turns.pop(0)


def _tool(name: str, **arguments: Any) -> LoopToolCall:
    return LoopToolCall(id=f"call-{name}-{uuid.uuid4().hex[:6]}", name=name, arguments=arguments)


def _all_message_text(messages: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, str):
            parts.append(content)
    return "\n".join(parts)


@pytest_asyncio.fixture
async def sf() -> Any:
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def workspace_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def founder_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest_asyncio.fixture
async def client(
    sf: async_sessionmaker[AsyncSession],
    workspace_id: uuid.UUID,
    founder_id: uuid.UUID,
) -> Any:
    app = create_app()

    def _ws() -> uuid.UUID:
        return workspace_id

    def _user_row() -> SimpleNamespace:
        return SimpleNamespace(id=founder_id)

    async def _session() -> Any:
        async with sf() as s:
            yield s

    app.dependency_overrides[get_current_user] = fake_current_user()
    app.dependency_overrides[get_workspace_id] = _ws
    app.dependency_overrides[get_current_user_row] = _user_row
    app.dependency_overrides[get_db_session] = _session

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_resolved_decision_seeds_b6_and_folds_into_b3_on_next_run(
    client: httpx.AsyncClient,
    sf: async_sessionmaker[AsyncSession],
    workspace_id: uuid.UUID,
    tmp_path: Path,
) -> None:
    """Phase A: resolve a decision (Postgres) on run #1, settle it to the
    vault. Phase B: drive RunOrchestrator for run #2 with the SAME workspace's
    retriever; assert the resolved-decision content shows up in BOTH
    (a) the LLM's first-turn messages (B6 seed) AND
    (b) the persisted VerificationResult.contract (B3 verify-time fold)."""
    # ----------------- Phase A: resolve + settle into the vault ------------
    async with sf() as s:
        run1 = ExecutionRun(
            id=uuid.uuid4(),
            workspace_id=workspace_id,
            status=RunStatus.RUNNING,
            payload={"intent_text": "pick the production database"},
            created_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC),
        )
        s.add(run1)
        await s.flush()
        decision = Decision(
            id=uuid.uuid4(),
            run_id=run1.id,
            workspace_id=workspace_id,
            decision="ask_user_question",
            payload={"question": "Which database should I target?"},
            status=DecisionStatus.PENDING,
        )
        s.add(decision)
        await s.commit()
        decision_id = decision.id

    # The founder answers via the production API surface.
    r = await client.post(
        f"/api/v1/checkpoints/{decision_id}/resolve",
        json={"answer": "Use Postgres for the analytics service"},
    )
    assert r.status_code == 200, r.text

    # Drain the settle worker → vault note for the resolved decision.
    worker = SettleWorker(
        session_factory=sf,
        sink=KnowledgeSettleSink(vault_root=tmp_path),
        config=SettleWorkerConfig(default_region="us-1"),
    )
    assert await worker.drain_once() == 1

    # ----------------- Phase B: NEW run with workspace-scoped retriever ----
    # The same seam the production factory wires (workers/run.py::_retriever_for).
    factory = KnowledgeFactory(
        region="us-1",
        workspace_id=str(workspace_id),
        vault_root=tmp_path,
    )
    retriever = factory.retriever()

    # Sanity: the retriever does surface the prior decision for an
    # overlapping signal (this is the precondition for the delta below).
    statements = await retriever.retrieve_for_signals(
        "Pick a database for the new analytics service"
    )
    assert statements, "expected at least one resolved-decision statement"
    seed_blob = "\n".join(statements)
    assert "Postgres" in seed_blob

    # The work LLM declares a JUDGE contract (so the B3 fold combines the
    # declared judge criterion with the retriever's canon as additional
    # judge criteria). The judge call passes.
    llm = _ScriptedLlm(
        [
            LoopTurn(
                content="declare judge contract + write file",
                tool_calls=(
                    _tool(
                        "declare_verification",
                        checks=[{"kind": "judge", "criteria": ["uses Postgres correctly"]}],
                    ),
                    _tool(
                        "file_write",
                        path="db.txt",
                        content="postgresql://analytics\n",
                    ),
                ),
            ),
            LoopTurn(content="implemented + Postgres wired", tool_calls=()),
            # The judge LLM call (tools=None) for declared + folded criteria.
            LoopTurn(
                content='{"passed": true, "reasoning": "follows Postgres pattern"}',
                tool_calls=(),
            ),
        ]
    )

    async with sf() as s:
        run2 = ExecutionRun(
            id=uuid.uuid4(),
            workspace_id=workspace_id,
            status=RunStatus.RUNNING,
            payload={
                "intent_text": "Pick a database for the new analytics service",
            },
            created_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC),
        )
        s.add(run2)
        await s.flush()
        run2_id = run2.id

        orch = RunOrchestrator(
            session=s,
            llm=llm,
            sandbox_manager=NoopSandboxManager(),
            retriever=retriever,
        )
        result = await orch.run(run=run2, workspace_dir=tmp_path)
        await s.commit()

    assert result.outcome == "verified", llm.calls

    # ----------------- The two B13 delta assertions ------------------------
    # (a) B6 seed delta — turn-1 messages contain the resolved decision text.
    assert llm.calls, "the LLM was never invoked"
    turn1_blob = _all_message_text(llm.calls[0]["messages"])
    assert "Postgres" in turn1_blob, (
        "the resolved decision did NOT appear in the second run's first-turn "
        f"messages (B6 seed broken). Saw:\n{turn1_blob}"
    )

    # (b) B3 verify-time fold delta — the persisted VerificationResult's
    # contract JSON contains the resolved decision text as a judge criterion.
    async with sf() as s:
        vr = (
            await s.execute(select(VerificationResult).where(VerificationResult.run_id == run2_id))
        ).scalar_one()
        assert vr.outcome is VerificationOutcome.PASSED
        contract_blob = str(vr.contract)
        assert "Postgres" in contract_blob, (
            "the resolved decision did NOT appear in the persisted verify "
            f"contract (B3 fold broken). Contract:\n{vr.contract}"
        )
        # And the cross-cutting invariant: a Deliverable for run 2 exists +
        # the WorkStep is PROVED+VERIFIED (this is the verified terminal, not
        # a fake-PROVED ship).
        deliverable = (
            await s.execute(select(Deliverable).where(Deliverable.run_id == run2_id))
        ).scalar_one()
        assert deliverable is not None
        step = (await s.execute(select(WorkStep).where(WorkStep.run_id == run2_id))).scalar_one()
        assert step.proof_state is ProofState.PROVED
        assert step.status is WorkStepStatus.VERIFIED
