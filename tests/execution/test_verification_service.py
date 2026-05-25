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

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.execution.db import (
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
from backend.execution.orchestrator import LoopTurn
from backend.execution.verifier.contract import (
    VerificationCheck,
    VerificationContract,
    parse_verification_contract,
)
from backend.execution.verifier.service import VerificationService
from backend.supervisor.sandbox.protocol import SandboxResult
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
        assert len(contract.command_checks) == 1
        assert contract.command_checks[0].command == "pytest -q"


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
# verify — command checks
# --------------------------------------------------------------------------


async def test_verify_command_pass_writes_passed_result() -> None:
    async with memory_session() as session:
        run = await _make_run(session)
        work_step, attempt = await _make_step_and_attempt(session, run)
        contract = VerificationContract(checks=(VerificationCheck(kind="command", command="true"),))
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
                "false": SandboxResult(exit_code=1, stdout="", stderr="boom", timed_out=False)
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
