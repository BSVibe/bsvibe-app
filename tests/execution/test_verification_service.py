"""VerificationService unit tests (execution layer, no HTTP, no orchestrator).

These exercise the standalone :class:`VerificationService` lifted out of
:class:`~backend.execution.orchestrator.RunOrchestrator` (Lift B2a). The
service runs the SAME verification machinery the native loop used to own
inline — contract assembly (declared + canon), command checks, the
LLM-judge, and the :class:`VerificationResult` write — so BOTH the native
loop and (later) the executor orchestrator can verify identically.

A fake ``box`` scripts ``exec`` / ``read_file`` deterministically; a stub
LLM scripts the judge verdict. No real model, no Docker.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import backend.workflow.application.verification_service as verification_service
from backend.knowledge.retrieval.knowledge_item import RetrievedKnowledge
from backend.workflow.application.agent_loop import LoopTurn
from backend.workflow.application.verification_service import (
    _UV_SYNC,
    RETRIEVED_KNOWLEDGE_RATIONALE,
    VerificationService,
)
from backend.workflow.domain.verifier_contract import (
    VerificationCheck,
    VerificationContract,
    parse_verification_contract,
)
from backend.workflow.infrastructure.db import (
    ExecutionRun,
    ExecutionRunActivity,
    RunAttempt,
    RunAttemptPhase,
    RunStatus,
    VerificationOutcome,
    VerificationResult,
    WorkStep,
    WorkStepStatus,
)
from backend.workflow.infrastructure.sandbox.protocol import SandboxResult
from tests._support import memory_session

# --------------------------------------------------------------------------
# Test doubles
# --------------------------------------------------------------------------


class StubLlm:
    """A deterministic judge LLM — pops the next scripted turn (FIFO)."""

    def __init__(self, turns: list[LoopTurn]) -> None:
        self._turns = list(turns)
        self.calls: list[dict[str, Any]] = []

    async def complete(
        self, *, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None
    ) -> LoopTurn:
        self.calls.append({"messages": list(messages), "tools": tools})
        if not self._turns:
            raise AssertionError("StubLlm exhausted — service requested an unscripted turn")
        return self._turns.pop(0)


class FakeBox:
    """A scripted :class:`SandboxSession`. ``exec`` returns a result per
    command from ``exec_map`` (default exit 0); ``read_file`` returns the
    bytes scripted in ``files``."""

    def __init__(
        self,
        *,
        exec_map: dict[str, SandboxResult] | None = None,
        files: dict[str, bytes] | None = None,
    ) -> None:
        self._exec_map = exec_map or {}
        self._files = files or {}
        self.exec_calls: list[str] = []
        self.read_calls: list[str] = []

    @property
    def workspace_mount(self) -> str:
        return "/workspace"

    async def exec(self, command: str, *, timeout_s: float, shell: bool = False) -> SandboxResult:
        self.exec_calls.append(command)
        return self._exec_map.get(
            command, SandboxResult(exit_code=0, stdout="ok", stderr="", timed_out=False)
        )

    async def read_file(self, rel_path: str, max_bytes: int) -> bytes:
        self.read_calls.append(rel_path)
        return self._files.get(rel_path, b"")

    async def write_file(self, rel_path: str, content: bytes) -> None:  # pragma: no cover
        self._files[rel_path] = content

    async def list_dir(self, rel_path: str) -> list[str]:  # pragma: no cover
        return list(self._files)


class StubRetriever:
    def __init__(
        self, patterns: list[str], *, items: list[RetrievedKnowledge] | None = None
    ) -> None:
        self._patterns = patterns
        self._items = items
        self.queried: list[str] = []

    async def retrieve_for_signals(self, signals: str) -> list[str]:
        self.queried.append(signals)
        return list(self._patterns)

    async def retrieve_structured(self, signals: str) -> list[RetrievedKnowledge]:
        self.queried.append(signals)
        if self._items is not None:
            return list(self._items)
        return [RetrievedKnowledge(text=p) for p in self._patterns]


async def _make_run(session: AsyncSession) -> ExecutionRun:
    run = ExecutionRun(
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        product_id=None,
        request_id=None,
        status=RunStatus.RUNNING,
        payload={"intent_text": "do the thing"},
    )
    session.add(run)
    await session.flush()
    return run


async def _make_step_and_attempt(
    session: AsyncSession, run: ExecutionRun
) -> tuple[WorkStep, RunAttempt]:
    work_step = WorkStep(
        id=uuid.uuid4(),
        run_id=run.id,
        workspace_id=run.workspace_id,
        title="t",
        status=WorkStepStatus.RUNNING,
        payload={},
    )
    attempt = RunAttempt(
        id=uuid.uuid4(),
        run_id=run.id,
        workspace_id=run.workspace_id,
        phase=RunAttemptPhase.VERIFYING,
        payload={},
    )
    session.add_all([work_step, attempt])
    await session.flush()
    return work_step, attempt


# --------------------------------------------------------------------------
# assemble_contract
# --------------------------------------------------------------------------


async def test_assemble_contract_returns_none_when_empty() -> None:
    async with memory_session() as session:
        svc = VerificationService(session=session, llm=StubLlm([]))
        contract = await svc.assemble_contract(
            declared_contract=None, written_paths=[], final_text=""
        )
        assert contract is None


async def test_assemble_contract_keeps_declared_checks() -> None:
    async with memory_session() as session:
        svc = VerificationService(session=session, llm=StubLlm([]))
        declared = {"checks": [{"kind": "command", "command": "pytest -q"}]}
        contract = await svc.assemble_contract(
            declared_contract=declared, written_paths=["a.py"], final_text="ran tests"
        )
        assert contract is not None
        cmds = [c.command for c in contract.command_checks]
        # The agent's declared check is preserved …
        assert "pytest -q" in cmds


async def test_assemble_contract_no_longer_appends_a_hardcoded_quality_bar() -> None:
    """The quality bar is no longer a hardcoded ``uv run ruff``/mypy append on
    ``.py`` files — it is DERIVED per-repo from the target's own manifests and
    run as the authoritative gate (``_run_derived_gate``), so the same coverage
    generalises across stacks. assemble_contract keeps only the agent's declared
    checks (its advisory attestation) + retrieved knowledge, not an invented
    Python bar."""
    async with memory_session() as session:
        svc = VerificationService(session=session, llm=StubLlm([]))
        declared = {
            "checks": [{"kind": "command", "command": "uv run pytest tests/common/test_x.py"}]
        }
        contract = await svc.assemble_contract(
            declared_contract=declared,
            written_paths=["backend/common/x.py", "tests/common/test_x.py"],
            final_text="",
        )
        assert contract is not None
        cmds = [c.command or "" for c in contract.command_checks]
        # the agent's own behavioral check is kept …
        assert any("pytest tests/common/test_x.py" in c for c in cmds)
        # … but NO hardcoded Python quality bar is manufactured here.
        assert not any(c.startswith("uv run ruff") for c in cmds)
        assert not any(c.startswith("uv run mypy") for c in cmds)


async def test_mandatory_gates_only_augment_a_real_attestation() -> None:
    """Mandatory gates AUGMENT a behavioral attestation; they never manufacture
    one. No declared command check → still routes to human review (None), so a
    lint-clean but untested change is not silently called verified."""
    async with memory_session() as session:
        svc = VerificationService(session=session, llm=StubLlm([]))
        contract = await svc.assemble_contract(
            declared_contract=None, written_paths=["backend/common/x.py"], final_text=""
        )
        assert contract is None


async def test_mandatory_gates_skipped_when_no_python_changed() -> None:
    """A non-Python change (docs, config) gets no ruff/mypy gate — they'd be
    no-ops or errors. Only the agent's declared check applies."""
    async with memory_session() as session:
        svc = VerificationService(session=session, llm=StubLlm([]))
        declared = {"checks": [{"kind": "command", "command": "test -f README.md"}]}
        contract = await svc.assemble_contract(
            declared_contract=declared, written_paths=["README.md", "docs/x.md"], final_text=""
        )
        assert contract is not None
        cmds = [c.command or "" for c in contract.command_checks]
        assert cmds == ["test -f README.md"]


async def test_assemble_contract_merges_declared_and_canon() -> None:
    retriever = StubRetriever(["always pin dependency versions"])
    async with memory_session() as session:
        svc = VerificationService(session=session, llm=StubLlm([]), retriever=retriever)
        declared = {"checks": [{"kind": "command", "command": "test -f deps.txt"}]}
        contract = await svc.assemble_contract(
            declared_contract=declared,
            written_paths=["deps.txt"],
            final_text="pinned deps",
        )
        assert contract is not None
        # One command (declared) + one judge (folded canon).
        assert len(contract.command_checks) == 1
        assert len(contract.judge_checks) == 1
        assert contract.judge_checks[0].criteria == ("always pin dependency versions",)
        assert retriever.queried, "retriever must be queried with change signals"


async def test_assemble_contract_persists_structured_knowledge_refs() -> None:
    """The folded judge check carries the retriever's STRUCTURED identity in
    ``knowledge_refs`` (serialized onto the contract JSON) so the delivery report
    deep-links each reference without re-deriving concept ids / note paths."""
    items = [
        RetrievedKnowledge(
            text="Prior decision — Q: Which DB? A: Postgres",
            kind="note",
            ref="garden/seedling/settle-db.md",
            label="Which DB?",
        ),
        RetrievedKnowledge(
            text="Idempotency-key — reuse the stored key.",
            kind="concept",
            ref="idempotency-key",
            label="Idempotency-key",
        ),
    ]
    retriever = StubRetriever([], items=items)
    async with memory_session() as session:
        svc = VerificationService(session=session, llm=StubLlm([]), retriever=retriever)
        contract = await svc.assemble_contract(
            declared_contract=None, written_paths=["db.py"], final_text="chose a database"
        )
        assert contract is not None
        judge = contract.judge_checks[0]
        # `criteria` stays the flat text (judge reads it; legacy readers work);
        # `knowledge_refs` carries the identity, serialized on to_dict().
        assert judge.criteria == (
            "Prior decision — Q: Which DB? A: Postgres",
            "Idempotency-key — reuse the stored key.",
        )
        persisted = judge.to_dict()
        assert persisted["rationale"] == RETRIEVED_KNOWLEDGE_RATIONALE
        assert persisted["knowledge_refs"] == [
            {
                "text": "Prior decision — Q: Which DB? A: Postgres",
                "kind": "note",
                "ref": "garden/seedling/settle-db.md",
                "label": "Which DB?",
            },
            {
                "text": "Idempotency-key — reuse the stored key.",
                "kind": "concept",
                "ref": "idempotency-key",
                "label": "Idempotency-key",
            },
        ]


async def test_assemble_contract_canon_only_when_no_declared() -> None:
    """A non-native caller passes declared_contract=None; canon alone still
    yields a contract (mirrors the native retriever fold)."""
    retriever = StubRetriever(["follow the existing logging convention"])
    async with memory_session() as session:
        svc = VerificationService(session=session, llm=StubLlm([]), retriever=retriever)
        contract = await svc.assemble_contract(
            declared_contract=None, written_paths=["x.py"], final_text="done"
        )
        assert contract is not None
        assert len(contract.judge_checks) == 1


async def test_assemble_contract_none_when_canon_empty_and_no_declared() -> None:
    retriever = StubRetriever([])
    async with memory_session() as session:
        svc = VerificationService(session=session, llm=StubLlm([]), retriever=retriever)
        contract = await svc.assemble_contract(
            declared_contract=None, written_paths=[], final_text=""
        )
        assert contract is None


# --------------------------------------------------------------------------
# B3 integration — the REAL factory retriever folds workspace canon into the
# assembled contract (it did NOT before B3, when the retriever was always None).
# Pairs with the empty-knowledge case: no canon → contract unchanged.
# --------------------------------------------------------------------------


async def test_real_factory_retriever_folds_canon_into_contract(tmp_path: Any) -> None:
    """A workspace WITH a matching canonical pattern → the assembled contract
    INCLUDES that canon as a judge criterion (the B3 delta)."""
    import uuid as _uuid
    from datetime import datetime as _dt

    from backend.knowledge import KnowledgeFactory
    from backend.knowledge.canonicalization import models as _models
    from backend.knowledge.canonicalization.store import NoteStore
    from backend.knowledge.graph.storage import FileSystemStorage

    vault_root = tmp_path / "vault"
    region, ws = "us-1", str(_uuid.uuid4())
    store = NoteStore(FileSystemStorage(vault_root / region / ws))
    await store.write_concept(
        _models.ConceptEntry(
            concept_id="dependency-pinning",
            path="concepts/active/dependency-pinning.md",
            display="Always pin dependency versions",
            aliases=[],
            created_at=_dt(2026, 5, 6),
            updated_at=_dt(2026, 5, 6),
        )
    )
    retriever = KnowledgeFactory(region=region, workspace_id=ws, vault_root=vault_root).retriever()

    async with memory_session() as session:
        svc = VerificationService(session=session, llm=StubLlm([]), retriever=retriever)
        contract = await svc.assemble_contract(
            declared_contract=None,
            written_paths=["requirements.txt"],
            final_text="updated dependency pinning in the lockfile",
        )
        assert contract is not None
        assert len(contract.judge_checks) == 1
        assert "Always pin dependency versions" in contract.judge_checks[0].criteria


async def test_real_factory_retriever_empty_workspace_leaves_contract_unchanged(
    tmp_path: Any,
) -> None:
    """An empty-knowledge workspace → the real retriever folds NOTHING, so a
    declared-only contract is identical to the no-retriever case."""
    import uuid as _uuid

    from backend.knowledge import KnowledgeFactory

    retriever = KnowledgeFactory(
        region="us-1", workspace_id=str(_uuid.uuid4()), vault_root=tmp_path / "vault"
    ).retriever()

    async with memory_session() as session:
        svc = VerificationService(session=session, llm=StubLlm([]), retriever=retriever)
        declared = {"checks": [{"kind": "command", "command": "pytest -q"}]}
        contract = await svc.assemble_contract(
            declared_contract=declared,
            # non-.py change → no mandatory lint/type gates, keeping this test
            # focused on the retriever folding nothing.
            written_paths=["notes.txt"],
            final_text="did a thing",
        )
        assert contract is not None
        # ONLY the declared command — no judge criterion folded in.
        assert len(contract.command_checks) == 1
        assert len(contract.judge_checks) == 0


# --------------------------------------------------------------------------
# verify — command checks
# --------------------------------------------------------------------------


async def test_verify_command_pass_writes_passed_result() -> None:
    async with memory_session() as session:
        run = await _make_run(session)
        work_step, attempt = await _make_step_and_attempt(session, run)
        contract = VerificationContract(checks=(VerificationCheck(kind="command", command="true"),))
        # No uv.lock in `files` → not a uv worktree → command runs bare.
        box = FakeBox(
            exec_map={"true": SandboxResult(exit_code=0, stdout="", stderr="", timed_out=False)}
        )
        svc = VerificationService(session=session, llm=StubLlm([]))
        vr = await svc.verify(
            run=run,
            work_step=work_step,
            attempt=attempt,
            contract=contract,
            box=box,
            written_paths=[],
            final_text="",
        )
        assert vr.outcome is VerificationOutcome.PASSED
        assert box.exec_calls == ["true"]
        # Persisted + a verify activity recorded.
        persisted = (await session.execute(select(VerificationResult))).scalar_one()
        assert persisted.id == vr.id
        activities = (await session.execute(select(ExecutionRunActivity))).scalars().all()
        assert any(a.activity_type == "verify" for a in activities)


async def test_verify_command_fail_writes_failed_result() -> None:
    async with memory_session() as session:
        run = await _make_run(session)
        work_step, attempt = await _make_step_and_attempt(session, run)
        contract = VerificationContract(
            checks=(VerificationCheck(kind="command", command="false"),)
        )
        box = FakeBox(
            exec_map={
                "false": SandboxResult(exit_code=1, stdout="", stderr="boom", timed_out=False),
            }
        )
        svc = VerificationService(session=session, llm=StubLlm([]))
        vr = await svc.verify(
            run=run,
            work_step=work_step,
            attempt=attempt,
            contract=contract,
            box=box,
            written_paths=[],
            final_text="",
        )
        assert vr.outcome is VerificationOutcome.FAILED


async def test_verify_command_only_makes_no_judge_call() -> None:
    async with memory_session() as session:
        run = await _make_run(session)
        work_step, attempt = await _make_step_and_attempt(session, run)
        contract = VerificationContract(checks=(VerificationCheck(kind="command", command="true"),))
        llm = StubLlm([])  # would raise if a judge call is made
        svc = VerificationService(session=session, llm=llm)
        vr = await svc.verify(
            run=run,
            work_step=work_step,
            attempt=attempt,
            contract=contract,
            box=FakeBox(),
            written_paths=[],
            final_text="",
        )
        assert vr.outcome is VerificationOutcome.PASSED
        assert llm.calls == []


# --------------------------------------------------------------------------
# verify — judge checks
# --------------------------------------------------------------------------


async def test_verify_judge_pass_reads_files_and_grades() -> None:
    async with memory_session() as session:
        run = await _make_run(session)
        work_step, attempt = await _make_step_and_attempt(session, run)
        contract = VerificationContract(
            checks=(VerificationCheck(kind="judge", criteria=("greets the world",)),)
        )
        box = FakeBox(files={"hello.txt": b"Hello, world"})
        llm = StubLlm([LoopTurn(content='{"passed": true, "reasoning": "ok"}')])
        svc = VerificationService(session=session, llm=llm)
        vr = await svc.verify(
            run=run,
            work_step=work_step,
            attempt=attempt,
            contract=contract,
            box=box,
            written_paths=["hello.txt"],
            final_text="wrote greeting",
        )
        assert vr.outcome is VerificationOutcome.PASSED
        # The judge call carried no tools, and the file was read for context.
        assert llm.calls[-1]["tools"] is None
        assert "hello.txt" in box.read_calls


async def test_verify_judge_fail_yields_failed() -> None:
    async with memory_session() as session:
        run = await _make_run(session)
        work_step, attempt = await _make_step_and_attempt(session, run)
        contract = VerificationContract(
            checks=(VerificationCheck(kind="judge", criteria=("has a valid proof",)),)
        )
        llm = StubLlm([LoopTurn(content='{"passed": false, "reasoning": "nope"}')])
        svc = VerificationService(session=session, llm=llm)
        vr = await svc.verify(
            run=run,
            work_step=work_step,
            attempt=attempt,
            contract=contract,
            box=FakeBox(),
            written_paths=[],
            final_text="",
        )
        assert vr.outcome is VerificationOutcome.FAILED


async def test_verify_command_pass_but_judge_fail_is_failed() -> None:
    """PASS gate = all_cmd_pass AND judge_pass — a single failing leg fails."""
    async with memory_session() as session:
        run = await _make_run(session)
        work_step, attempt = await _make_step_and_attempt(session, run)
        contract = VerificationContract(
            checks=(
                VerificationCheck(kind="command", command="true"),
                VerificationCheck(kind="judge", criteria=("must be perfect",)),
            )
        )
        llm = StubLlm([LoopTurn(content='{"passed": false}')])
        svc = VerificationService(session=session, llm=llm)
        vr = await svc.verify(
            run=run,
            work_step=work_step,
            attempt=attempt,
            contract=contract,
            box=FakeBox(),
            written_paths=[],
            final_text="",
        )
        assert vr.outcome is VerificationOutcome.FAILED


async def test_verify_retriever_added_judge_is_advisory_when_command_passes() -> None:
    """Lift E39 — when the agent's own command_check PASSES and the only
    judge check is the one ``assemble_contract`` adds from BSage retrieval
    (rationale=RETRIEVED_KNOWLEDGE_RATIONALE — the agent did not declare
    it), a hallucinating judge verdict must NOT flip the outcome to FAILED.

    The E38 dogfood (run df66a253, 2026-06-17) caught the failure mode:
    the agent's command_check ``git show HEAD:… | grep -q '…docstring text'``
    passed exit-0 (proving the change landed), but the retriever-added
    judge — which got criteria like "Verification" / "Git" / "Related
    note — …plan-mode…" and only the first 8 KB of a 13 KB file — said
    "no docstring at all" and the run fell into a verification_failed
    spin until the round budget ran out.

    The agent's command_check IS the agent's primary attestation. The
    retriever's judge is a secondary "do these BSage patterns also seem
    satisfied" advisory. When the primary passes, the secondary becomes
    informational (still recorded on the result) but not gating.
    """
    from backend.workflow.application.verification_service import (
        RETRIEVED_KNOWLEDGE_RATIONALE,
    )

    async with memory_session() as session:
        run = await _make_run(session)
        work_step, attempt = await _make_step_and_attempt(session, run)
        contract = VerificationContract(
            checks=(
                VerificationCheck(kind="command", command="true"),
                VerificationCheck(
                    kind="judge",
                    criteria=("Verification", "Git"),
                    rationale=RETRIEVED_KNOWLEDGE_RATIONALE,
                ),
            )
        )
        # Empty script: StubLlm raises if a judge call is made — proving the
        # advisory judge is SKIPPED entirely (F6), not merely demoted.
        llm = StubLlm([])
        svc = VerificationService(session=session, llm=llm)
        vr = await svc.verify(
            run=run,
            work_step=work_step,
            attempt=attempt,
            contract=contract,
            box=FakeBox(),
            written_paths=[],
            final_text="",
        )
        # Outcome PASSES because the agent's command attestation passed.
        assert vr.outcome is VerificationOutcome.PASSED
        # F6 — the cheap LLM-judge is NOT run for an advisory retriever-only
        # check (it reliably hallucinated "files don't exist" while the command
        # passed — dogfood). Instead the result honestly records that it was
        # skipped as advisory; the criteria still surface as Delivery-Report
        # references via the contract.
        judge_blob = vr.result["judge"]
        assert judge_blob is not None
        assert judge_blob.get("advisory") is True
        assert judge_blob.get("skipped") == "advisory_retrieval_only"
        assert "passed" not in judge_blob


async def test_verify_agent_declared_judge_still_gates_outcome() -> None:
    """Lift E39 — guardrail: only the *retriever-added* judge becomes
    advisory. If the AGENT explicitly declared a judge check (empty
    rationale — see ``parse_verification_contract``), it still gates
    the outcome the same way as before. Agent attestation respects
    what the agent chose to stake the verification on.
    """
    async with memory_session() as session:
        run = await _make_run(session)
        work_step, attempt = await _make_step_and_attempt(session, run)
        contract = VerificationContract(
            checks=(
                VerificationCheck(kind="command", command="true"),
                # Empty rationale = agent declared this judge, NOT the retriever.
                VerificationCheck(
                    kind="judge",
                    criteria=("must contain doctring",),
                    rationale="",
                ),
            )
        )
        llm = StubLlm([LoopTurn(content='{"passed": false}')])
        svc = VerificationService(session=session, llm=llm)
        vr = await svc.verify(
            run=run,
            work_step=work_step,
            attempt=attempt,
            contract=contract,
            box=FakeBox(),
            written_paths=[],
            final_text="",
        )
        assert vr.outcome is VerificationOutcome.FAILED


async def test_verify_retrieved_knowledge_excluded_from_gating_judge() -> None:
    """The retriever-added judge (rationale=RETRIEVED_KNOWLEDGE_RATIONALE) is
    REFERENCES, not acceptance criteria — even when the agent ALSO declared its
    own judge, only the agent's criteria are graded. The retrieved criteria
    (folded in by loose semantic similarity, often unrelated to the task) must
    never pollute the gating grade. Dogfood dd2bd3a3: a rate-limiter run got
    "Toss Payments webhook HMAC" retrieved criteria and could never pass."""
    from backend.workflow.application.verification_service import (
        RETRIEVED_KNOWLEDGE_RATIONALE,
    )

    async with memory_session() as session:
        run = await _make_run(session)
        work_step, attempt = await _make_step_and_attempt(session, run)
        contract = VerificationContract(
            checks=(
                VerificationCheck(kind="command", command="true"),
                VerificationCheck(kind="judge", criteria=("agent wants X",), rationale=""),
                VerificationCheck(
                    kind="judge",
                    criteria=("Toss Payments webhook HMAC — unrelated to this task",),
                    rationale=RETRIEVED_KNOWLEDGE_RATIONALE,
                ),
            )
        )
        seen: dict[str, list[str]] = {}

        async def _fake_judge(criteria, written_paths, final_text, box):  # noqa: ANN001, ANN202
            seen["criteria"] = list(criteria)
            return {"passed": True}

        svc = VerificationService(session=session, llm=StubLlm([]))
        svc._run_judge = _fake_judge  # type: ignore[method-assign]
        vr = await svc.verify(
            run=run,
            work_step=work_step,
            attempt=attempt,
            contract=contract,
            box=FakeBox(),
            written_paths=[],
            final_text="",
        )
        assert vr.outcome is VerificationOutcome.PASSED
        # ONLY the agent's own criterion is graded; the retrieved one is excluded.
        assert seen["criteria"] == ["agent wants X"]


async def test_verify_parses_declared_then_verifies_round_trip() -> None:
    """End-to-end through the service surface: parse → assemble → verify."""
    async with memory_session() as session:
        run = await _make_run(session)
        work_step, attempt = await _make_step_and_attempt(session, run)
        declared = parse_verification_contract(
            {"checks": [{"kind": "command", "command": "test -f marker"}]}
        )
        assert declared is not None
        svc = VerificationService(session=session, llm=StubLlm([]))
        contract = await svc.assemble_contract(
            declared_contract={"checks": [{"kind": "command", "command": "test -f marker"}]},
            written_paths=["marker"],
            final_text="done",
        )
        assert contract is not None
        vr = await svc.verify(
            run=run,
            work_step=work_step,
            attempt=attempt,
            contract=contract,
            box=FakeBox(),
            written_paths=["marker"],
            final_text="done",
        )
        assert vr.outcome is VerificationOutcome.PASSED


# --------------------------------------------------------------------------
# _run_command_checks — project venv for uv worktrees (issue #361)
# --------------------------------------------------------------------------


def _cmd_contract(command: str) -> VerificationContract:
    return VerificationContract(checks=(VerificationCheck(kind="command", command=command),))


async def test_command_checks_sync_and_prefix_venv_for_uv_project() -> None:
    """uv worktree (uv.lock present) → `uv sync` once, then each command runs
    with the project venv prepended to PATH so a plain `python -m pytest`
    resolves project deps."""
    box = FakeBox(
        files={"uv.lock": b"# lockfile"},
        exec_map={_UV_SYNC: SandboxResult(exit_code=0, stdout="", stderr="", timed_out=False)},
    )
    async with memory_session() as session:
        svc = VerificationService(session=session, llm=StubLlm([]))
        results = await svc._run_command_checks(  # noqa: SLF001
            _cmd_contract("python -m pytest tests/x.py -v"), box
        )
    assert _UV_SYNC in box.exec_calls  # synced once
    assert "uv.lock" in box.read_calls  # detected via the lockfile read
    prefixed = 'export PATH="/workspace/.venv/bin:$PATH"; python -m pytest tests/x.py -v'
    assert prefixed in box.exec_calls  # command ran inside the venv
    assert results[0]["command"] == "python -m pytest tests/x.py -v"  # recorded command is clean
    assert results[0]["passed"] is True


async def test_command_checks_no_sync_or_prefix_when_not_uv_project() -> None:
    """No uv.lock → not a uv project → no sync, command runs bare."""
    box = FakeBox()  # no files → read_file("uv.lock") returns b""
    async with memory_session() as session:
        svc = VerificationService(session=session, llm=StubLlm([]))
        await svc._run_command_checks(_cmd_contract("npm test"), box)  # noqa: SLF001
    assert _UV_SYNC not in box.exec_calls
    assert box.exec_calls == ["npm test"]  # bare, no PATH prefix


async def test_command_checks_no_prefix_when_uv_sync_fails() -> None:
    """uv.lock present but `uv sync` failed → run command bare (honest fail,
    never a silent pass via a half-built venv)."""
    box = FakeBox(
        files={"uv.lock": b"# lockfile"},
        exec_map={_UV_SYNC: SandboxResult(exit_code=1, stdout="", stderr="boom", timed_out=False)},
    )
    async with memory_session() as session:
        svc = VerificationService(session=session, llm=StubLlm([]))
        await svc._run_command_checks(  # noqa: SLF001
            _cmd_contract("python -m pytest tests/x.py"), box
        )
    assert _UV_SYNC in box.exec_calls  # attempted
    assert "python -m pytest tests/x.py" in box.exec_calls  # ran bare
    assert not any(c.startswith("export PATH=") for c in box.exec_calls)


# --------------------------------------------------------------------------
# I2 — outcome demonstration (independent verifier plans + runs probes; the
# verdict is a DETERMINISTIC observation==expectation comparison)
# --------------------------------------------------------------------------

_PROBE_CMD = "python -c 'from backend.common.x import x; print(x())'"


def _plan_turn(command: str, contains: list[str]) -> LoopTurn:
    """A demonstration-plan reply (the planner emits JSON, not code)."""
    probe = {"name": "exercise x", "command": command, "expect_stdout_contains": contains}
    return LoopTurn(content=json.dumps({"probes": [probe]}))


async def test_demonstration_contradiction_fails_verification() -> None:
    """The independent verifier exercises the FINISHED deliverable. When the
    probe runs and the intended result is NOT observed, verification fails —
    even though the worker's own command passed (breaking self-grading and
    catching garbage that a pure intent-judge would wave through, Q-2)."""
    async with memory_session() as session:
        run = await _make_run(session)
        work_step, attempt = await _make_step_and_attempt(session, run)
        # Verifier plans: exercise x(), expect it to print "42".
        llm = StubLlm([_plan_turn(_PROBE_CMD, ["42"])])
        svc = VerificationService(session=session, llm=llm)
        # The deliverable prints "1", not "42" → observation contradicts.
        box = FakeBox(
            files={"backend/common/x.py": b"def x() -> int:\n    return 1\n"},
            exec_map={
                "true": SandboxResult(exit_code=0, stdout="", stderr="", timed_out=False),
                _PROBE_CMD: SandboxResult(exit_code=0, stdout="1\n", stderr="", timed_out=False),
            },
        )
        contract = VerificationContract(checks=(VerificationCheck(kind="command", command="true"),))
        vr = await svc.verify(
            run=run,
            work_step=work_step,
            attempt=attempt,
            contract=contract,
            box=box,
            written_paths=["backend/common/x.py"],
            final_text="",
        )
        assert vr.outcome is VerificationOutcome.FAILED
        assert llm.calls, "the independent demonstration planner must be called"
        assert vr.result["outcome_demonstration"]["verdict"] == "failed"


async def test_demonstration_match_keeps_verified() -> None:
    async with memory_session() as session:
        run = await _make_run(session)
        work_step, attempt = await _make_step_and_attempt(session, run)
        llm = StubLlm([_plan_turn(_PROBE_CMD, ["42"])])
        svc = VerificationService(session=session, llm=llm)
        # The deliverable prints "42" → observation matches the expectation.
        box = FakeBox(
            files={"backend/common/x.py": b"def x() -> int:\n    return 42\n"},
            exec_map={
                "true": SandboxResult(exit_code=0, stdout="", stderr="", timed_out=False),
                _PROBE_CMD: SandboxResult(exit_code=0, stdout="42\n", stderr="", timed_out=False),
            },
        )
        contract = VerificationContract(checks=(VerificationCheck(kind="command", command="true"),))
        vr = await svc.verify(
            run=run,
            work_step=work_step,
            attempt=attempt,
            contract=contract,
            box=box,
            written_paths=["backend/common/x.py"],
            final_text="",
        )
        assert vr.outcome is VerificationOutcome.PASSED
        assert vr.result["outcome_demonstration"]["verdict"] == "demonstrated"


async def test_demonstration_runs_unconditionally() -> None:
    """I2 is a safety net — it runs on EVERY verify with code changes, no
    opt-out flag. A plainly-constructed service still plans + runs probes."""
    async with memory_session() as session:
        run = await _make_run(session)
        work_step, attempt = await _make_step_and_attempt(session, run)
        llm = StubLlm([_plan_turn(_PROBE_CMD, ["42"])])
        svc = VerificationService(session=session, llm=llm)  # no flag — always on
        box = FakeBox(
            files={"backend/common/x.py": b"def x() -> int:\n    return 42\n"},
            exec_map={
                "true": SandboxResult(exit_code=0, stdout="", stderr="", timed_out=False),
                _PROBE_CMD: SandboxResult(exit_code=0, stdout="42\n", stderr="", timed_out=False),
            },
        )
        contract = VerificationContract(checks=(VerificationCheck(kind="command", command="true"),))
        vr = await svc.verify(
            run=run,
            work_step=work_step,
            attempt=attempt,
            contract=contract,
            box=box,
            written_paths=["backend/common/x.py"],
            final_text="",
        )
        assert vr.outcome is VerificationOutcome.PASSED
        assert llm.calls, "the planner runs with no opt-in flag"


async def test_demonstration_unavailable_probe_does_not_false_fail() -> None:
    """Best-effort (founder decision #1): a probe that could not EXERCISE the
    deliverable (wrong import path / missing command) is unavailable, not a
    contradiction — good code is never false-failed. Verdict → undemonstrable,
    verification still PASSES on the worker's own command."""
    async with memory_session() as session:
        run = await _make_run(session)
        work_step, attempt = await _make_step_and_attempt(session, run)
        llm = StubLlm([_plan_turn(_PROBE_CMD, ["42"])])
        svc = VerificationService(session=session, llm=llm)
        box = FakeBox(
            files={"backend/common/x.py": b"def x() -> int:\n    return 42\n"},
            exec_map={
                "true": SandboxResult(exit_code=0, stdout="", stderr="", timed_out=False),
                _PROBE_CMD: SandboxResult(
                    exit_code=1, stdout="", stderr="ModuleNotFoundError: no", timed_out=False
                ),
            },
        )
        contract = VerificationContract(checks=(VerificationCheck(kind="command", command="true"),))
        vr = await svc.verify(
            run=run,
            work_step=work_step,
            attempt=attempt,
            contract=contract,
            box=box,
            written_paths=["backend/common/x.py"],
            final_text="",
        )
        assert vr.outcome is VerificationOutcome.PASSED
        assert vr.result["outcome_demonstration"]["verdict"] == "undemonstrable"


async def test_demonstration_no_probes_is_undemonstrable_not_fail() -> None:
    """A deliverable the verifier cannot reduce to an executable probe (returns
    an empty plan) is honestly UNDEMONSTRABLE — a downgrade, never a fail."""
    async with memory_session() as session:
        run = await _make_run(session)
        work_step, attempt = await _make_step_and_attempt(session, run)
        llm = StubLlm([LoopTurn(content='{"probes": []}')])
        svc = VerificationService(session=session, llm=llm)
        box = FakeBox(
            files={"backend/common/x.py": b"def x() -> int:\n    return 42\n"},
            exec_map={"true": SandboxResult(exit_code=0, stdout="", stderr="", timed_out=False)},
        )
        contract = VerificationContract(checks=(VerificationCheck(kind="command", command="true"),))
        vr = await svc.verify(
            run=run,
            work_step=work_step,
            attempt=attempt,
            contract=contract,
            box=box,
            written_paths=["backend/common/x.py"],
            final_text="",
        )
        assert vr.outcome is VerificationOutcome.PASSED
        assert vr.result["outcome_demonstration"]["verdict"] == "undemonstrable"


async def test_demonstration_skipped_for_prose_only_change() -> None:
    """A prose/data change (no exercisable CODE file) yields no demonstration —
    the planner is never even called, and the verdict blob is absent."""
    async with memory_session() as session:
        run = await _make_run(session)
        work_step, attempt = await _make_step_and_attempt(session, run)
        llm = StubLlm([])  # would raise if the planner were called
        svc = VerificationService(session=session, llm=llm)
        box = FakeBox(
            exec_map={"true": SandboxResult(exit_code=0, stdout="", stderr="", timed_out=False)}
        )
        contract = VerificationContract(checks=(VerificationCheck(kind="command", command="true"),))
        vr = await svc.verify(
            run=run,
            work_step=work_step,
            attempt=attempt,
            contract=contract,
            box=box,
            written_paths=["README.md", "docs/x.md"],
            final_text="",
        )
        assert vr.outcome is VerificationOutcome.PASSED
        assert vr.result["outcome_demonstration"] is None
        assert llm.calls == []


class _HangingLlm:
    """A judge/author LLM that never returns in time — models a hung executor
    CLI task (a chat-shaped completion dispatched to an executor account)."""

    async def complete(
        self, *, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None
    ) -> LoopTurn:
        await asyncio.sleep(10)
        return LoopTurn(content='{"passed": true}')  # pragma: no cover — never reached


async def test_run_judge_times_out_to_non_pass(monkeypatch: Any) -> None:
    # A hung executor LLM must NOT stall the run in verify — _run_judge bounds
    # the call and returns an explicit NON-pass (never a silent pass) on timeout.
    monkeypatch.setattr(verification_service, "_VERIFY_LLM_TIMEOUT_S", 0.05)
    async with memory_session() as session:
        svc = VerificationService(session=session, llm=_HangingLlm())
        verdict = await svc._run_judge(
            criteria=["it works"],
            written_paths=[],
            final_text="did the thing",
            box=FakeBox(),
        )
    assert verdict["passed"] is False
    assert "timed out" in verdict["reasoning"].lower()


# --------------------------------------------------------------------------
# I3 — scope discipline (flag changed files unrelated to the intent; SURFACE,
# never block). Gated to a product run with a real worktree (the durable diff).
# --------------------------------------------------------------------------


class TestScopeCheck:
    async def _seed(self, session, tmp_path, monkeypatch, *, product=True, worktree=True):
        run = await _make_run(session)  # payload intent_text="do the thing"
        if product:
            run.product_id = uuid.uuid4()
        wt = tmp_path / str(run.id)
        wt.mkdir(parents=True)
        if worktree:
            (wt / ".git").write_text("gitdir: /elsewhere\n")
        import backend.storage.product_workspace as pw

        monkeypatch.setattr(pw, "run_worktree_path", lambda _rid: wt)
        return run

    async def test_flags_unrelated_files(self, tmp_path, monkeypatch):
        async with memory_session() as session:
            run = await self._seed(session, tmp_path, monkeypatch)
            llm = StubLlm(
                [LoopTurn(content='{"flagged": ["backend/eventbus.py"], "reasoning": "unrelated"}')]
            )
            svc = VerificationService(session=session, llm=llm)
            blob = await svc._run_scope_check(run, ["README.md", "backend/eventbus.py"])
            assert blob is not None
            assert blob["verdict"] == "flagged"
            assert blob["flagged_paths"] == ["backend/eventbus.py"]
            assert blob["candidates"] == 2

    async def test_clean_when_all_related(self, tmp_path, monkeypatch):
        async with memory_session() as session:
            run = await self._seed(session, tmp_path, monkeypatch)
            svc = VerificationService(
                session=session, llm=StubLlm([LoopTurn(content='{"flagged": []}')])
            )
            blob = await svc._run_scope_check(run, ["README.md"])
            assert blob is not None
            assert blob["verdict"] == "clean"
            assert blob["flagged_paths"] == []

    async def test_none_for_non_product_run(self, tmp_path, monkeypatch):
        async with memory_session() as session:
            run = await self._seed(session, tmp_path, monkeypatch, product=False)
            llm = StubLlm([])  # would raise if the judge were called
            svc = VerificationService(session=session, llm=llm)
            assert await svc._run_scope_check(run, ["backend/x.py"]) is None
            assert llm.calls == []

    async def test_none_without_real_worktree(self, tmp_path, monkeypatch):
        async with memory_session() as session:
            run = await self._seed(session, tmp_path, monkeypatch, worktree=False)
            svc = VerificationService(session=session, llm=StubLlm([]))
            assert await svc._run_scope_check(run, ["backend/x.py"]) is None

    async def test_lockfile_only_change_has_no_candidates(self, tmp_path, monkeypatch):
        async with memory_session() as session:
            run = await self._seed(session, tmp_path, monkeypatch)
            llm = StubLlm([])  # pre-filter drops the lockfile → no judge call
            svc = VerificationService(session=session, llm=llm)
            assert await svc._run_scope_check(run, ["uv.lock"]) is None
            assert llm.calls == []

    async def test_invented_path_is_dropped(self, tmp_path, monkeypatch):
        # The judge cannot flag a path outside the actual candidate set.
        async with memory_session() as session:
            run = await self._seed(session, tmp_path, monkeypatch)
            svc = VerificationService(
                session=session,
                llm=StubLlm([LoopTurn(content='{"flagged": ["does/not/exist.py"]}')]),
            )
            blob = await svc._run_scope_check(run, ["backend/x.py"])
            assert blob is not None
            assert blob["flagged_paths"] == []
            assert blob["verdict"] == "clean"

    async def test_unparseable_verdict_returns_none(self, tmp_path, monkeypatch):
        async with memory_session() as session:
            run = await self._seed(session, tmp_path, monkeypatch)
            svc = VerificationService(session=session, llm=StubLlm([LoopTurn(content="not json")]))
            assert await svc._run_scope_check(run, ["backend/x.py"]) is None

    async def test_scope_flag_surfaces_in_verify_without_failing(self, tmp_path, monkeypatch):
        """End-to-end through verify(): a scope flag is RECORDED in the result but
        the outcome stays PASSED — founder decision #3, surface not block."""
        import backend.storage.product_workspace as pw

        async with memory_session() as session:
            run = await self._seed(session, tmp_path, monkeypatch)  # product + worktree, no CI
            work_step, attempt = await _make_step_and_attempt(session, run)
            # W2 merge step fires for product+worktree runs — stub it clean.
            monkeypatch.setattr(pw, "commit_worktree", AsyncMock(), raising=False)
            monkeypatch.setattr(
                pw,
                "merge_main_into_worktree",
                AsyncMock(return_value=SimpleNamespace(status="clean", conflict_paths=[])),
                raising=False,
            )
            # README task, but an unrelated doc got touched → flagged. Prose-only
            # paths → no demonstration; no CI in the worktree → no project gate.
            llm = StubLlm(
                [LoopTurn(content='{"flagged": ["docs/unrelated.md"], "reasoning": "x"}')]
            )
            svc = VerificationService(session=session, llm=llm)
            contract = VerificationContract(
                checks=(VerificationCheck(kind="command", command="true"),)
            )
            vr = await svc.verify(
                run=run,
                work_step=work_step,
                attempt=attempt,
                contract=contract,
                box=FakeBox(),
                written_paths=["README.md", "docs/unrelated.md"],
                final_text="added a README line",
            )
            assert vr.outcome is VerificationOutcome.PASSED  # flag never fails
            assert vr.result["scope"]["verdict"] == "flagged"
            assert vr.result["scope"]["flagged_paths"] == ["docs/unrelated.md"]


# --------------------------------------------------------------------------
# I3 — honesty ladder grade recorded on the verdict (redesign §4)
# --------------------------------------------------------------------------


class TestHonestyGrade:
    async def _seed_product_worktree(
        self, session, tmp_path, monkeypatch, *, ci_yaml=None, manifest=None
    ):
        run = await _make_run(session)
        run.product_id = uuid.uuid4()
        wt = tmp_path / str(run.id)
        wt.mkdir(parents=True)
        (wt / ".git").write_text("gitdir: /elsewhere\n")
        if ci_yaml is not None:
            (wt / ".github" / "workflows").mkdir(parents=True)
            (wt / ".github" / "workflows" / "ci.yml").write_text(ci_yaml)
        if manifest is not None:
            (wt / manifest).write_text("")  # a stack manifest → gate_expected
        import backend.storage.product_workspace as pw

        monkeypatch.setattr(pw, "run_worktree_path", lambda _rid: wt)
        monkeypatch.setattr(pw, "commit_worktree", AsyncMock(), raising=False)
        monkeypatch.setattr(
            pw,
            "merge_main_into_worktree",
            AsyncMock(return_value=SimpleNamespace(status="clean", conflict_paths=[])),
            raising=False,
        )
        return run

    async def test_grade_d_greenfield_no_stack_is_not_gate_expected(self, tmp_path, monkeypatch):
        """An early/greenfield product (no manifest) with no gate is graded D but
        gate_expected=False — legitimately gateless, so the ratchet auto-proceeds."""
        async with memory_session() as session:
            run = await self._seed_product_worktree(session, tmp_path, monkeypatch)  # empty repo
            work_step, attempt = await _make_step_and_attempt(session, run)
            # scope, then the deriver: no toolchain here → not-applicable.
            llm = StubLlm(
                [
                    LoopTurn(content='{"flagged": []}'),
                    LoopTurn(content='{"applicable": false, "commands": []}'),
                ]
            )
            svc = VerificationService(session=session, llm=llm)
            contract = VerificationContract(
                checks=(VerificationCheck(kind="command", command="true"),)
            )
            vr = await svc.verify(
                run=run,
                work_step=work_step,
                attempt=attempt,
                contract=contract,
                box=FakeBox(),
                written_paths=["README.md"],
                final_text="",
            )
            assert vr.outcome is VerificationOutcome.PASSED
            assert vr.result["honesty_grade"] == "D"
            assert vr.result["gate_expected"] is False

    async def test_grade_d_real_project_no_gate_is_gate_expected(self, tmp_path, monkeypatch):
        """A repo with a stack manifest but no runnable gate is graded D AND
        gate_expected=True — a real project that should declare a gate → review."""
        async with memory_session() as session:
            run = await self._seed_product_worktree(
                session, tmp_path, monkeypatch, manifest="pyproject.toml"
            )
            work_step, attempt = await _make_step_and_attempt(session, run)
            # A real toolchain (applicable) but the deriver produced no runnable
            # command → a gate was EXPECTED yet none ran → grade D, gate_expected.
            llm = StubLlm(
                [
                    LoopTurn(content='{"flagged": []}'),
                    LoopTurn(content='{"applicable": true, "commands": []}'),
                ]
            )
            svc = VerificationService(session=session, llm=llm)
            contract = VerificationContract(
                checks=(VerificationCheck(kind="command", command="true"),)
            )
            vr = await svc.verify(
                run=run,
                work_step=work_step,
                attempt=attempt,
                contract=contract,
                box=FakeBox(),
                written_paths=["README.md"],
                final_text="",
            )
            assert vr.outcome is VerificationOutcome.PASSED
            assert vr.result["honesty_grade"] == "D"
            assert vr.result["gate_expected"] is True

    async def test_grade_b_when_gate_passes(self, tmp_path, monkeypatch):
        """A product run whose DERIVED gate RAN and passed earns B (one strong
        leg) even without a demonstration."""
        async with memory_session() as session:
            run = await self._seed_product_worktree(
                session, tmp_path, monkeypatch, manifest="pyproject.toml"
            )
            work_step, attempt = await _make_step_and_attempt(session, run)
            # scope, then the deriver returns a runnable command → FakeBox exit 0.
            llm = StubLlm(
                [
                    LoopTurn(content='{"flagged": []}'),
                    LoopTurn(
                        content='{"applicable": true, "commands": [{"command": "ruff check ."}]}'
                    ),
                ]
            )
            svc = VerificationService(session=session, llm=llm)
            contract = VerificationContract(
                checks=(VerificationCheck(kind="command", command="true"),)
            )
            vr = await svc.verify(
                run=run,
                work_step=work_step,
                attempt=attempt,
                contract=contract,
                box=FakeBox(),
                written_paths=["README.md"],
                final_text="",
            )
            assert vr.outcome is VerificationOutcome.PASSED
            assert vr.result["honesty_grade"] == "B"

    async def test_grade_none_for_non_product_run(self, tmp_path, monkeypatch):
        """A non-product Direct run has no repo-gate ladder — grade is None."""
        async with memory_session() as session:
            run = await _make_run(session)  # product_id=None
            work_step, attempt = await _make_step_and_attempt(session, run)
            svc = VerificationService(session=session, llm=StubLlm([]))
            contract = VerificationContract(
                checks=(VerificationCheck(kind="command", command="true"),)
            )
            vr = await svc.verify(
                run=run,
                work_step=work_step,
                attempt=attempt,
                contract=contract,
                box=FakeBox(),
                written_paths=[],
                final_text="",
            )
            assert vr.outcome is VerificationOutcome.PASSED
            assert vr.result["honesty_grade"] is None

    async def test_grade_a_when_derived_gate_passes_and_demonstrated(self, tmp_path, monkeypatch):
        """Both strong legs — the DERIVED gate ran and passed AND the outcome was
        demonstrated — earns A, the strongest, objective grade."""
        async with memory_session() as session:
            run = await self._seed_product_worktree(
                session, tmp_path, monkeypatch, manifest="pyproject.toml"
            )
            work_step, attempt = await _make_step_and_attempt(session, run)
            # code change → demonstration; then scope; then the derived gate.
            llm = StubLlm(
                [
                    LoopTurn(content='{"probes": [{"name": "ok", "command": "true"}]}'),
                    LoopTurn(content='{"flagged": []}'),
                    LoopTurn(
                        content='{"applicable": true, "commands": [{"command": "ruff check calc.py"}]}'
                    ),
                ]
            )
            svc = VerificationService(session=session, llm=llm)
            contract = VerificationContract(
                checks=(VerificationCheck(kind="command", command="true"),)
            )
            vr = await svc.verify(
                run=run,
                work_step=work_step,
                attempt=attempt,
                contract=contract,
                box=FakeBox(),  # probe `true` + derived `ruff check calc.py` both exit 0
                written_paths=["calc.py"],
                final_text="",
            )
            assert vr.outcome is VerificationOutcome.PASSED
            assert vr.result["outcome_demonstration"]["verdict"] == "demonstrated"
            assert vr.result["honesty_grade"] == "A"

    async def test_false_positive_zero_passed_has_no_failed_authoritative_command(
        self, tmp_path, monkeypatch
    ):
        """The integrity invariant (Q-2 / false-positive-0): a PASSED verdict can
        NEVER carry a FAILED command in the authoritative derived gate. An
        unavailable (127) command doesn't fail; a real failure would flip the
        verdict — so a green run is always backed by a clean gate."""
        async with memory_session() as session:
            run = await self._seed_product_worktree(
                session, tmp_path, monkeypatch, manifest="pyproject.toml"
            )
            work_step, attempt = await _make_step_and_attempt(session, run)
            llm = StubLlm(
                [
                    LoopTurn(content='{"flagged": []}'),
                    LoopTurn(
                        content=(
                            '{"applicable": true, "commands": ['
                            '{"command": "ruff check x.py"}, {"command": "mypy x.py"}]}'
                        )
                    ),
                ]
            )
            svc = VerificationService(session=session, llm=llm)
            contract = VerificationContract(
                checks=(VerificationCheck(kind="command", command="true"),)
            )
            box = FakeBox(
                exec_map={
                    "mypy x.py": SandboxResult(
                        exit_code=127, stdout="", stderr="mypy: not found", timed_out=False
                    )
                    # ruff check x.py → default exit 0 (passed)
                }
            )
            vr = await svc.verify(
                run=run,
                work_step=work_step,
                attempt=attempt,
                contract=contract,
                box=box,
                written_paths=["README.md"],
                final_text="",
            )
            assert vr.outcome is VerificationOutcome.PASSED
            statuses = [c["status"] for c in vr.result["derived_gate"]["commands"]]
            assert "failed" not in statuses  # false-positive-0: no failed authoritative command
            assert "passed" in statuses and "unavailable" in statuses


class TestDerivedGate:
    """The repo's OWN verification gate, DERIVED by an LLM grounded in its
    manifests and then RUN deterministically — the general replacement for the
    hardcoded `uv run ruff`/mypy bar and the per-stack gate_discovery detectors.
    The verdict is exit codes, never the model's opinion; a missing tool
    (exit 127) is unavailable, never a false-fail (I2 — not yet wired into the
    verdict)."""

    async def _seed(self, session, tmp_path, monkeypatch, *, product=True):
        run = await _make_run(session)
        if product:
            run.product_id = uuid.uuid4()
        wt = tmp_path / str(run.id)
        wt.mkdir(parents=True)
        (wt / ".git").write_text("gitdir: /elsewhere\n")
        import backend.storage.product_workspace as pw

        monkeypatch.setattr(pw, "run_worktree_path", lambda _rid: wt)
        return run

    def _gate_turn(self, commands, *, applicable=True):
        return LoopTurn(content=json.dumps({"applicable": applicable, "commands": commands}))

    async def test_runs_derived_commands_and_passes(self, tmp_path, monkeypatch):
        async with memory_session() as session:
            llm = StubLlm(
                [self._gate_turn([{"command": "uv run ruff check money.py", "kind": "quality"}])]
            )
            svc = VerificationService(session=session, llm=llm)
            run = await self._seed(session, tmp_path, monkeypatch)
            box = FakeBox()  # default exit 0
            blob = await svc._run_derived_gate(run, box, ["money.py"])
            assert blob is not None
            assert blob["origin"] == "derived" and blob["passed"] is True
            assert blob["commands"][0]["status"] == "passed"
            assert "uv run ruff check money.py" in box.exec_calls

    async def test_unavailable_command_is_not_a_failure(self, tmp_path, monkeypatch):
        async with memory_session() as session:
            llm = StubLlm([self._gate_turn([{"command": "cargo clippy"}])])
            svc = VerificationService(session=session, llm=llm)
            run = await self._seed(session, tmp_path, monkeypatch)
            box = FakeBox(
                exec_map={
                    "cargo clippy": SandboxResult(
                        exit_code=127, stdout="", stderr="not found", timed_out=False
                    )
                }
            )
            blob = await svc._run_derived_gate(run, box, ["src/lib.rs"])
            assert blob is not None
            assert blob["commands"][0]["status"] == "unavailable"
            assert blob["passed"] is True  # 127 = tool absent here, not a real failure

    async def test_real_failure_fails_the_gate(self, tmp_path, monkeypatch):
        async with memory_session() as session:
            llm = StubLlm([self._gate_turn([{"command": "uv run ruff check money.py"}])])
            svc = VerificationService(session=session, llm=llm)
            run = await self._seed(session, tmp_path, monkeypatch)
            box = FakeBox(
                exec_map={
                    "uv run ruff check money.py": SandboxResult(
                        exit_code=1, stdout="", stderr="I001", timed_out=False
                    )
                }
            )
            blob = await svc._run_derived_gate(run, box, ["money.py"])
            assert blob is not None
            assert blob["commands"][0]["status"] == "failed" and blob["passed"] is False

    async def test_grounds_deriver_in_repo_manifests(self, tmp_path, monkeypatch):
        async with memory_session() as session:
            llm = StubLlm([self._gate_turn([{"command": "uv run ruff check money.py"}])])
            svc = VerificationService(session=session, llm=llm)
            run = await self._seed(session, tmp_path, monkeypatch)
            box = FakeBox(files={"pyproject.toml": b"[tool.ruff]\nline-length = 100\n"})
            await svc._run_derived_gate(run, box, ["money.py"])
            sent = "\n".join(m["content"] for m in llm.calls[0]["messages"])
            assert "[tool.ruff]" in sent  # the repo's manifest grounded the deriver
            assert "money.py" in sent

    async def test_not_applicable_runs_nothing(self, tmp_path, monkeypatch):
        async with memory_session() as session:
            llm = StubLlm([self._gate_turn([], applicable=False)])
            svc = VerificationService(session=session, llm=llm)
            run = await self._seed(session, tmp_path, monkeypatch)
            box = FakeBox()
            blob = await svc._run_derived_gate(run, box, ["design.md"])
            assert blob is not None
            assert blob["applicable"] is False and blob["commands"] == []
            assert blob["passed"] is True and box.exec_calls == []

    async def test_none_when_not_a_product_worktree(self, tmp_path, monkeypatch):
        async with memory_session() as session:
            svc = VerificationService(session=session, llm=StubLlm([]))
            run = await self._seed(session, tmp_path, monkeypatch, product=False)
            assert await svc._run_derived_gate(run, FakeBox(), ["x.py"]) is None

    async def test_none_on_deriver_hiccup(self, tmp_path, monkeypatch):
        async with memory_session() as session:
            svc = VerificationService(session=session, llm=StubLlm([]))  # exhausted → raises
            run = await self._seed(session, tmp_path, monkeypatch)
            assert await svc._run_derived_gate(run, FakeBox(), ["x.py"]) is None


class TestVerdictWiring:
    """I3 — the LLM-derived gate is the AUTHORITATIVE command gate; the agent's
    declared commands are advisory. An invented command that fails on the sandbox
    (the F7 loop) no longer false-fails the run; a real gate failure still FAILS
    it (Q-2: garbage never passes 'verified')."""

    async def _seed(self, session, tmp_path, monkeypatch, *, manifest="pyproject.toml"):
        run = await _make_run(session)
        run.product_id = uuid.uuid4()
        wt = tmp_path / str(run.id)
        wt.mkdir(parents=True)
        (wt / ".git").write_text("gitdir: /elsewhere\n")
        if manifest:
            (wt / manifest).write_text("")
        import backend.storage.product_workspace as pw

        monkeypatch.setattr(pw, "run_worktree_path", lambda _rid: wt)
        monkeypatch.setattr(pw, "commit_worktree", AsyncMock(), raising=False)
        monkeypatch.setattr(
            pw,
            "merge_main_into_worktree",
            AsyncMock(return_value=SimpleNamespace(status="clean", conflict_paths=[])),
            raising=False,
        )
        return run

    def _turns(self, derived_commands, *, applicable=True):
        # verify()'s LLM call order for a product+worktree code change with a
        # command-only contract: demonstration → scope → derived gate.
        return [
            LoopTurn(content='{"probes": []}'),  # demonstration → undemonstrable
            LoopTurn(content='{"flagged": []}'),  # scope → clean
            LoopTurn(content=json.dumps({"applicable": applicable, "commands": derived_commands})),
        ]

    async def test_invented_command_does_not_false_fail_when_derived_gate_passes(
        self, tmp_path, monkeypatch
    ):
        async with memory_session() as session:
            run = await self._seed(session, tmp_path, monkeypatch)
            work_step, attempt = await _make_step_and_attempt(session, run)
            svc = VerificationService(
                session=session,
                llm=StubLlm(self._turns([{"command": "uv run ruff check money.py"}])),
            )
            # The AGENT declared an env-incompatible command (F7): it RUNS and
            # fails (uv rejects an undefined extra) — but it is advisory now.
            contract = VerificationContract(
                checks=(
                    VerificationCheck(
                        kind="command", command="uv run --extra dev ruff check money.py"
                    ),
                )
            )
            box = FakeBox(
                exec_map={
                    "uv run --extra dev ruff check money.py": SandboxResult(
                        exit_code=2,
                        stdout="",
                        stderr="error: Extra `dev` is not defined",
                        timed_out=False,
                    )
                    # the DERIVED `uv run ruff check money.py` passes (FakeBox default exit 0)
                }
            )
            vr = await svc.verify(
                run=run,
                work_step=work_step,
                attempt=attempt,
                contract=contract,
                box=box,
                written_paths=["money.py"],
                final_text="",
            )
            assert vr.outcome is VerificationOutcome.PASSED  # invented cmd did not gate
            assert vr.result["derived_gate"]["passed"] is True
            # the agent's broken command is still RECORDED (advisory) for the surface
            assert any(not r["passed"] for r in vr.result["command_results"])

    async def test_real_derived_gate_failure_still_fails(self, tmp_path, monkeypatch):
        async with memory_session() as session:
            run = await self._seed(session, tmp_path, monkeypatch)
            work_step, attempt = await _make_step_and_attempt(session, run)
            svc = VerificationService(
                session=session,
                llm=StubLlm(self._turns([{"command": "uv run ruff check money.py"}])),
            )
            contract = VerificationContract(
                checks=(VerificationCheck(kind="command", command="true"),)  # agent cmd passes
            )
            box = FakeBox(
                exec_map={
                    "uv run ruff check money.py": SandboxResult(
                        exit_code=1, stdout="", stderr="I001 import unsorted", timed_out=False
                    )
                }
            )
            vr = await svc.verify(
                run=run,
                work_step=work_step,
                attempt=attempt,
                contract=contract,
                box=box,
                written_paths=["money.py"],
                final_text="",
            )
            assert vr.outcome is VerificationOutcome.FAILED  # a real gate failure fails (Q-2)
            assert vr.result["derived_gate"]["passed"] is False
