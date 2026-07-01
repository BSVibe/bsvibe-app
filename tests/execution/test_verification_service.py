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
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import backend.workflow.application.verification_service as verification_service
from backend.workflow.application.agent_loop import LoopTurn
from backend.workflow.application.verification_service import (
    _UV_SYNC,
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
    def __init__(self, patterns: list[str]) -> None:
        self._patterns = patterns
        self.queried: list[str] = []

    async def retrieve_for_signals(self, signals: str) -> list[str]:
        self.queried.append(signals)
        return list(self._patterns)


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


async def test_assemble_contract_appends_mandatory_quality_gates() -> None:
    """L1 — ``verified`` must mean the project's deterministic quality bar
    (lint/format/type) held on the changed files, not just the one command the
    agent chose to declare. So assemble_contract appends mandatory ruff / ruff
    format / mypy command checks on the changed ``.py`` files, regardless of the
    declared contract — closing the self-attestation gap (an agent that only ran
    a narrow pytest can't pass with unformatted / lint-broken / mistyped code).
    """
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
        # agent's own behavioral check kept
        assert any("pytest tests/common/test_x.py" in c for c in cmds)
        # mandatory deterministic gates appended on the changed .py files
        assert any(c.startswith("uv run ruff check") and "backend/common/x.py" in c for c in cmds)
        assert any("ruff format --check" in c and "tests/common/test_x.py" in c for c in cmds)
        assert any(c.startswith("uv run mypy") and "backend/common/x.py" in c for c in cmds)


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
# L2 — independent acceptance check (separate verifier authors + runs a test)
# --------------------------------------------------------------------------


async def test_independent_acceptance_failure_fails_verification() -> None:
    """A SEPARATE verifier authors a test from the INTENT and runs it. A failing
    independent test fails verification (→ human review) even though the worker's
    own command passed — breaking the self-grading circularity."""
    async with memory_session() as session:
        run = await _make_run(session)
        work_step, attempt = await _make_step_and_attempt(session, run)
        llm = StubLlm([LoopTurn(content="```python\ndef test_spec():\n    assert False\n```")])
        svc = VerificationService(session=session, llm=llm)
        box = FakeBox(
            files={"backend/common/x.py": b"def x() -> int:\n    return 1\n"},
            exec_map={
                "true": SandboxResult(exit_code=0, stdout="", stderr="", timed_out=False),
                "uv run pytest tests/_bsvibe_independent_acceptance.py -q": SandboxResult(
                    exit_code=1, stdout="1 failed", stderr="", timed_out=False
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
        assert vr.outcome is VerificationOutcome.FAILED
        assert llm.calls, "the independent author LLM must be called"
        assert any(r.get("independent") for r in vr.result["command_results"])


async def test_independent_acceptance_pass_keeps_verified() -> None:
    async with memory_session() as session:
        run = await _make_run(session)
        work_step, attempt = await _make_step_and_attempt(session, run)
        llm = StubLlm([LoopTurn(content="```python\ndef test_spec():\n    assert True\n```")])
        svc = VerificationService(session=session, llm=llm)
        # The independent pytest command is absent from exec_map → FakeBox
        # default exit 0 → the authored test passes.
        box = FakeBox(
            files={"backend/common/x.py": b"def x() -> int:\n    return 1\n"},
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
        assert any(r.get("independent") for r in vr.result["command_results"])


async def test_independent_acceptance_runs_unconditionally() -> None:
    """L2 is a safety net — it runs on EVERY verify, with no opt-out flag. A
    plainly-constructed service still authors + runs the independent test."""
    async with memory_session() as session:
        run = await _make_run(session)
        work_step, attempt = await _make_step_and_attempt(session, run)
        llm = StubLlm([LoopTurn(content="```python\ndef test_spec():\n    assert True\n```")])
        svc = VerificationService(session=session, llm=llm)  # no flag — always on
        box = FakeBox(
            files={"backend/common/x.py": b"def x() -> int:\n    return 1\n"},
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
        assert any(r.get("independent") for r in vr.result["command_results"])
        assert llm.calls, "the author runs with no opt-in flag"


async def test_independent_acceptance_broken_test_is_discarded() -> None:
    """Robust to a weak verify model: an UNUSABLE authored test (collection
    error, pytest exit 2) must NOT false-fail good code. It is retried once for
    self-repair and, if still unusable, DISCARDED — never a gate."""
    async with memory_session() as session:
        run = await _make_run(session)
        work_step, attempt = await _make_step_and_attempt(session, run)
        llm = StubLlm(
            [
                LoopTurn(content="```python\nimport nope\n```"),  # initial author
                LoopTurn(content="```python\nimport still_nope\n```"),  # self-repair retry
            ]
        )
        svc = VerificationService(session=session, llm=llm)
        box = FakeBox(
            files={"backend/common/x.py": b"def x() -> int:\n    return 1\n"},
            exec_map={
                "true": SandboxResult(exit_code=0, stdout="", stderr="", timed_out=False),
                "uv run pytest tests/_bsvibe_independent_acceptance.py -q": SandboxResult(
                    exit_code=2, stdout="", stderr="ModuleNotFoundError: nope", timed_out=False
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
        # broken test discarded → the worker's own command alone gates → PASSED
        assert vr.outcome is VerificationOutcome.PASSED
        assert not any(r.get("independent") for r in vr.result["command_results"])
        # the self-repair retry fired (two author calls)
        assert len(llm.calls) == 2


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
# L-I1b — project gate (run the repo's OWN static gate, fail-closed)
# --------------------------------------------------------------------------


class TestProjectGate:
    """Verification runs the repo's OWN discovered static gate (whole-repo
    lint/format/type/contracts) and fail-closes on a genuine gate failure — so
    'verified' means 'passes the target's own definition of done', not a narrow
    changed-files subset (findings 2026-07-01, #413)."""

    async def _seed(self, session, tmp_path, monkeypatch, *, ci_yaml, product=True):
        run = await _make_run(session)
        if product:
            run.product_id = uuid.uuid4()
        wt = tmp_path / str(run.id)
        (wt / ".github" / "workflows").mkdir(parents=True)
        (wt / ".git").write_text("gitdir: /elsewhere\n")
        (wt / ".github" / "workflows" / "ci.yml").write_text(ci_yaml)
        import backend.storage.product_workspace as pw

        monkeypatch.setattr(pw, "run_worktree_path", lambda _rid: wt)
        return run

    async def test_gate_failure_marks_gate_not_passed(self, tmp_path, monkeypatch):
        async with memory_session() as session:
            svc = VerificationService(session=session, llm=StubLlm([]))
            run = await self._seed(
                session,
                tmp_path,
                monkeypatch,
                ci_yaml="jobs:\n  j:\n    steps:\n      - run: ruff check .\n",
            )
            box = FakeBox(
                exec_map={
                    "ruff check .": SandboxResult(
                        exit_code=1, stdout="", stderr="E501", timed_out=False
                    )
                }
            )
            blob = await svc._run_project_gate(run, box)
            assert blob is not None
            assert blob["origin"] == "github-actions"
            assert blob["passed"] is False
            assert blob["commands"][0]["status"] == "failed"

    async def test_gate_passes_when_all_checks_pass(self, tmp_path, monkeypatch):
        async with memory_session() as session:
            svc = VerificationService(session=session, llm=StubLlm([]))
            run = await self._seed(
                session,
                tmp_path,
                monkeypatch,
                ci_yaml="jobs:\n  j:\n    steps:\n      - run: ruff check .\n",
            )
            blob = await svc._run_project_gate(run, FakeBox())  # default exit 0
            assert blob is not None and blob["passed"] is True
            assert blob["commands"][0]["status"] == "passed"

    async def test_setup_and_dynamic_test_steps_are_skipped(self, tmp_path, monkeypatch):
        async with memory_session() as session:
            svc = VerificationService(session=session, llm=StubLlm([]))
            run = await self._seed(
                session,
                tmp_path,
                monkeypatch,
                ci_yaml=(
                    "jobs:\n  j:\n    steps:\n"
                    "      - run: uv sync --all-extras\n"
                    "      - run: ruff check .\n"
                    "      - run: uv run pytest -q\n"
                ),
            )
            box = FakeBox()
            blob = await svc._run_project_gate(run, box)
            assert blob is not None
            # only the static check runs; setup + dynamic test are deferred
            assert [c["command"] for c in blob["commands"]] == ["ruff check ."]
            assert "uv sync --all-extras" not in box.exec_calls
            assert "uv run pytest -q" not in box.exec_calls

    async def test_unavailable_command_is_not_a_failure(self, tmp_path, monkeypatch):
        async with memory_session() as session:
            svc = VerificationService(session=session, llm=StubLlm([]))
            run = await self._seed(
                session,
                tmp_path,
                monkeypatch,
                ci_yaml="jobs:\n  j:\n    steps:\n      - run: eslint .\n",
            )
            box = FakeBox(
                exec_map={
                    "eslint .": SandboxResult(
                        exit_code=127, stdout="", stderr="eslint: not found", timed_out=False
                    )
                }
            )
            blob = await svc._run_project_gate(run, box)
            assert blob is not None
            assert blob["commands"][0]["status"] == "unavailable"
            assert blob["passed"] is True  # couldn't run here ≠ failed

    async def test_none_when_not_a_product_worktree(self, tmp_path, monkeypatch):
        async with memory_session() as session:
            svc = VerificationService(session=session, llm=StubLlm([]))
            run = await self._seed(
                session,
                tmp_path,
                monkeypatch,
                ci_yaml="jobs:\n  j:\n    steps:\n      - run: ruff check .\n",
                product=False,
            )
            assert await svc._run_project_gate(run, FakeBox()) is None

    async def test_none_when_repo_declares_no_static_gate(self, tmp_path, monkeypatch):
        async with memory_session() as session:
            svc = VerificationService(session=session, llm=StubLlm([]))
            run = await self._seed(
                session,
                tmp_path,
                monkeypatch,
                ci_yaml="jobs:\n  j:\n    steps:\n      - run: uv run pytest -q\n",
            )
            # only a dynamic test → nothing runnable as a static gate here
            assert await svc._run_project_gate(run, FakeBox()) is None
