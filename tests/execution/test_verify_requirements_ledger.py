"""The requirements ledger at the verification gate — through ``VerificationService.verify``.

Nothing here calls the ledger functions directly: a run carrying a ledger is verified the
way the drive loop verifies it, and the outcome, the feedback the agent reads next round
and the stored verdict rows are asserted.
"""

from __future__ import annotations

from typing import Any

from backend.workflow.application.agent_loop import LoopTurn
from backend.workflow.application.verification_service import VerificationService
from backend.workflow.domain.requirements import LEDGER_RESULT_KEY, REQUIREMENTS_KEY
from backend.workflow.domain.verification_feedback import render_verification_failure
from backend.workflow.domain.verifier_contract import VerificationCheck, VerificationContract
from backend.workflow.infrastructure.db import VerificationOutcome
from backend.workflow.infrastructure.sandbox import SandboxResult
from tests._support import memory_session
from tests.execution.test_verification_service import (
    FakeBox,
    StubLlm,
    _make_run,
    _make_step_and_attempt,
)

_LEDGER = [
    {"id": "R1", "text": "회전 경로를 추가한다", "kind": "do", "source": "issue"},
    {"id": "R2", "text": "다른 워크스페이스는 404", "kind": "guard", "source": "issue"},
    {"id": "R3", "text": "PWA 는 건드리지 마라", "kind": "constraint", "source": "issue"},
]
# What git reports: one tracked edit (made through the shell — no write tool saw it) and one
# new test file.
_STATUS = " M backend/api/connectors.py\n?? tests/api/test_rotate.py\n"
_FILES = {
    "backend/api/connectors.py": b"def rotate():\n    ...\n",
    "tests/api/test_rotate.py": b"def test_foreign_404():\n    ...\n",
}
_OK = '{"passed": true, "reasoning": "ok"}'
_NO = '{"passed": false, "reasoning": "only the happy path"}'
_FULL_EVIDENCE = (
    {"requirement_id": "R1", "paths": ["backend/api/connectors.py"]},
    {"requirement_id": "R2", "tests": ["tests/api/test_rotate.py::test_foreign_404"]},
)


def _box() -> FakeBox:
    return FakeBox(
        exec_map={
            "git status --porcelain": SandboxResult(
                exit_code=0, stdout=_STATUS, stderr="", timed_out=False
            )
        },
        files=dict(_FILES),
    )


def _judge(*answers: str) -> StubLlm:
    return StubLlm([LoopTurn(content=a, tool_calls=[]) for a in answers])


async def _verify(
    evidence: tuple[dict[str, Any], ...],
    llm: StubLlm,
    *,
    ledger: list[dict[str, Any]] | None = _LEDGER,
    command: str = "true",
    box: FakeBox | None = None,
    rounds: int = 1,
) -> list[Any]:
    """Verify ``rounds`` times on one run; return each round's VerificationResult."""
    results = []
    async with memory_session() as session:
        run = await _make_run(session)
        if ledger is not None:
            run.payload = {**run.payload, REQUIREMENTS_KEY: ledger}
        work_step, attempt = await _make_step_and_attempt(session, run)
        contract = VerificationContract(
            checks=(VerificationCheck(kind="command", command=command),),
            requirement_evidence=evidence,
        )
        svc = VerificationService(session=session, llm=llm)
        for _ in range(rounds):
            results.append(
                await svc.verify(
                    run=run,
                    work_step=work_step,
                    attempt=attempt,
                    contract=contract,
                    box=box or _box(),
                    written_paths=[],
                    final_text="done",
                )
            )
            await session.flush()
    return results


def _rows(vr: Any) -> dict[str, dict[str, Any]]:
    return {r["id"]: r for r in vr.result[LEDGER_RESULT_KEY]}


async def test_an_item_without_evidence_fails_and_the_feedback_names_it() -> None:
    [vr] = await _verify(
        ({"requirement_id": "R1", "paths": ["backend/api/connectors.py"]},), _judge(_OK)
    )

    assert vr.outcome is VerificationOutcome.FAILED
    assert _rows(vr)["R2"]["status"] == "missing"
    feedback = render_verification_failure(vr.result)
    assert "R2" in feedback and "다른 워크스페이스는 404" in feedback


async def test_every_owed_item_evidenced_passes_and_a_constraint_is_only_noted() -> None:
    llm = _judge(_OK, _OK)
    [vr] = await _verify(_FULL_EVIDENCE, llm)

    assert vr.outcome is VerificationOutcome.PASSED
    rows = _rows(vr)
    assert (rows["R1"]["status"], rows["R2"]["status"], rows["R3"]["status"]) == (
        "done",
        "done",
        "noted",
    )
    assert len(llm.calls) == 2  # one judge per grounded item, none for the constraint


async def test_a_shell_edit_counts_because_the_diff_is_asked_of_git() -> None:
    # written_paths is empty: no write tool saw backend/api/connectors.py change.
    [vr] = await _verify(_FULL_EVIDENCE, _judge(_OK, _OK))

    assert _rows(vr)["R1"]["paths"] == ["backend/api/connectors.py"]


async def test_the_item_judge_sees_the_cited_file_and_never_the_rest() -> None:
    llm = _judge(_OK, _OK)
    await _verify(_FULL_EVIDENCE, llm)

    r1_prompt = next(
        m["content"]
        for call in llm.calls
        for m in call["messages"]
        if m["role"] == "user" and "회전 경로를 추가한다" in m["content"]
    )
    assert "backend/api/connectors.py" in r1_prompt
    assert "tests/api/test_rotate.py" not in r1_prompt


async def test_a_rejected_item_fails_the_run_with_the_judges_reason() -> None:
    [vr] = await _verify(_FULL_EVIDENCE, _judge(_OK, _NO))

    assert vr.outcome is VerificationOutcome.FAILED
    assert "rejected" in {r["status"] for r in _rows(vr).values()}
    assert "only the happy path" in render_verification_failure(vr.result)


async def test_no_judge_runs_when_another_check_already_failed() -> None:
    llm = _judge()  # any judge call would exhaust the stub
    box = _box()
    box._exec_map["false"] = SandboxResult(exit_code=1, stdout="", stderr="boom", timed_out=False)
    [vr] = await _verify(_FULL_EVIDENCE, llm, command="false", box=box)

    assert vr.outcome is VerificationOutcome.FAILED
    assert llm.calls == []
    assert _rows(vr)["R1"]["status"] == "pending"


async def test_a_verdict_is_reused_while_its_evidence_is_unchanged() -> None:
    llm = _judge(_OK, _OK)  # enough for ONE round only
    first, second = await _verify(_FULL_EVIDENCE, llm, rounds=2)

    assert first.outcome is VerificationOutcome.PASSED
    assert second.outcome is VerificationOutcome.PASSED
    assert len(llm.calls) == 2


async def test_a_deferred_item_with_a_reason_passes() -> None:
    evidence = (
        {"requirement_id": "R1", "paths": ["backend/api/connectors.py"]},
        {"requirement_id": "R2", "status": "deferred", "note": "needs the founder's call"},
    )
    [vr] = await _verify(evidence, _judge(_OK))

    assert vr.outcome is VerificationOutcome.PASSED
    assert _rows(vr)["R2"]["status"] == "deferred"


async def test_a_run_with_no_ledger_is_unchanged() -> None:
    [vr] = await _verify((), _judge(), ledger=None)

    assert vr.outcome is VerificationOutcome.PASSED
    assert LEDGER_RESULT_KEY not in vr.result


class _FlakyJudge(StubLlm):
    """Raises on the first call, then answers like StubLlm."""

    def __init__(self, turns: list[LoopTurn]) -> None:
        super().__init__(turns)
        self.failed = False

    async def complete(self, *, messages: list[dict[str, Any]], tools: Any) -> LoopTurn:
        if not self.failed:
            self.failed = True
            self.calls.append({"messages": list(messages), "tools": tools})
            raise RuntimeError("model blip")
        return await super().complete(messages=messages, tools=tools)


async def test_a_judge_blip_is_not_remembered_as_a_verdict() -> None:
    evidence = ({"requirement_id": "R1", "paths": ["backend/api/connectors.py"]},)
    ledger = [_LEDGER[0]]
    llm = _FlakyJudge([LoopTurn(content=_OK, tool_calls=[])])
    first, second = await _verify(evidence, llm, ledger=ledger, rounds=2)

    assert _rows(first)["R1"]["status"] == "unclear"
    assert _rows(second)["R1"]["status"] == "done"  # judged again, not the blip reused
