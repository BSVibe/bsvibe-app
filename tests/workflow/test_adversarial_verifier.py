"""The adversarial verifier — through ``VerificationService.verify`` and the harness directly.

Only the LLM is a stub. The verdict on every finding is the HARNESS's (a repro's exit code /
output); the model's own opinion of its finding never counts. Stage 1 is report-only.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from backend.workflow.application.adversarial_verifier import run_adversarial_round
from backend.workflow.application.agent_loop import LoopToolCall, LoopTurn
from backend.workflow.application.run_detail import _tally_adversarial
from backend.workflow.application.safe_mode_ledger import _adversarial_summary
from backend.workflow.application.verification_service import VerificationService
from backend.workflow.domain.adversarial import (
    SCRATCH_ROOT,
    judge_command,
    judge_test,
    parse_findings,
    write_refusal,
)
from backend.workflow.domain.declared_checks import CI_PREFIX
from backend.workflow.domain.verifier_contract import VerificationCheck, VerificationContract
from backend.workflow.infrastructure.db import VerificationOutcome
from backend.workflow.infrastructure.sandbox import SandboxResult
from tests._support import memory_session
from tests.execution.test_verification_service import (
    StubLlm,
    _make_run,
    _make_step_and_attempt,
)
from tests.workflow.test_artifact_review import _GitBox

_TEST_REPRO = {"kind": "test", "path": f"{SCRATCH_ROOT}/run-1/test_bug.py"}


def _finding(repro: dict[str, Any] | None = None, **extra: Any) -> dict[str, Any]:
    return {
        "severity": "high",
        "claim": "cleanup is never wired",
        "file": "backend/svc.py",
        "line": 3,
        "repro": repro or _TEST_REPRO,
        **extra,
    }


def _answer(*findings: dict[str, Any]) -> LoopTurn:
    return LoopTurn(content=json.dumps({"findings": list(findings)}), tool_calls=())


def _tool(name: str, **args: Any) -> LoopTurn:
    return LoopTurn(
        content="", tool_calls=(LoopToolCall(id=f"c-{name}", name=name, arguments=args),)
    )


def _res(code: int = 0, out: str = "", *, timed_out: bool = False) -> SandboxResult:
    return SandboxResult(exit_code=code, stdout=out, stderr="", timed_out=timed_out)


class _AdvBox(_GitBox):
    """A git box whose pytest / repro commands are scripted and whose writes are recorded."""

    def __init__(self, *, pytest: SandboxResult | None = None, **scripted: SandboxResult) -> None:
        super().__init__()
        self.pytest = pytest or _res(0, "1 passed")
        self.scripted = scripted
        self.writes: dict[str, bytes] = {}
        self.commands: list[str] = []
        self.status_out = ""
        self.status_calls = 0

    async def exec(self, command: str, **kwargs: Any) -> SandboxResult:
        self.commands.append(command)
        if "status --porcelain" in command:
            self.status_calls += 1
            # the first call is the "before" snapshot; later ones see whatever a stray write left
            return _res(0, "" if self.status_calls == 1 else self.status_out)
        if "pytest" in command:
            return self.pytest
        bare = command.removeprefix(CI_PREFIX)
        if bare in self.scripted:
            return self.scripted[bare]
        return await super().exec(command, **kwargs)

    async def write_file(self, rel_path: str, content: bytes) -> None:
        self.writes[rel_path] = content


def _run() -> Any:
    return SimpleNamespace(
        id="run-1",
        payload={"intent_text": "do it"},
        usage_prompt_tokens=0,
        usage_completion_tokens=0,
    )


async def _round(llm: StubLlm, box: _AdvBox, surface: str = "code") -> dict[str, Any]:
    async def release() -> None:
        return None

    return await run_adversarial_round(
        llm=llm, run=_run(), box=box, baseline="base123", surface=surface, release=release
    )


# -- pure rules -------------------------------------------------------------------------------


def test_parse_drops_findings_without_repro_and_caps_at_five() -> None:
    no_repro = {"severity": "high", "claim": "vibes"}
    bad_command = _finding({"kind": "command", "command": "ls"})  # no expect
    good = [_finding() for _ in range(7)]
    parsed = parse_findings(json.dumps({"findings": [no_repro, bad_command, *good]}))
    assert parsed is not None
    assert len(parsed) == 5
    assert all(f.repro.kind == "test" for f in parsed)
    assert parse_findings("not json at all") is None


def test_the_harness_decides_from_exit_codes_and_output_only() -> None:
    # pytest: 1 = a test failed (the defect shown); 0 passed; 2 / 5 = could not run
    assert judge_test(exit_code=1, failed_code=1, timed_out=False)
    assert not judge_test(exit_code=0, failed_code=1, timed_out=False)
    assert not judge_test(exit_code=5, failed_code=1, timed_out=False)
    assert not judge_test(exit_code=1, failed_code=1, timed_out=True)
    assert judge_command({"exit_code": "nonzero"}, exit_code=3, output="", timed_out=False)
    assert not judge_command({"exit_code": "nonzero"}, exit_code=0, output="", timed_out=False)
    assert not judge_command({"contains": "boom"}, exit_code=0, output="fine", timed_out=False)
    assert not judge_command({"contains": "boom"}, exit_code=0, output="boom", timed_out=True)


def test_only_a_test_file_inside_the_scratch_dir_may_be_written() -> None:
    scratch = f"{SCRATCH_ROOT}/abc"
    assert write_refusal(f"{scratch}/test_a.py", scratch) is None
    assert write_refusal("backend/svc.py", scratch) is not None
    assert write_refusal(f"{scratch}/helper.py", scratch) is not None  # not a test file
    assert write_refusal(f"{scratch}/../../backend/test_x.py", scratch) is not None
    assert write_refusal("/etc/test_x.py", scratch) is not None


# -- the harness, end to end -----------------------------------------------------------------


async def test_failing_repro_test_is_confirmed() -> None:
    # the model CLAIMS nothing is confirmed either way: the pytest exit code decides
    box = _AdvBox(pytest=_res(1, "1 failed"))
    blob = await _round(StubLlm([_answer(_finding())]), box)

    assert blob["status"] == "done"
    assert (blob["confirmed"], blob["unconfirmed"]) == (1, 0)
    assert blob["findings"][0]["confirmed"] is True
    assert "1 failed" in blob["findings"][0]["output"]


async def test_passing_repro_is_unconfirmed_even_if_the_model_says_confirmed() -> None:
    box = _AdvBox(pytest=_res(0, "1 passed"))
    blob = await _round(StubLlm([_answer(_finding(confirmed=True))]), box)

    assert (blob["confirmed"], blob["unconfirmed"]) == (0, 1)
    assert blob["findings"][0]["confirmed"] is False


async def test_a_repro_that_cannot_run_is_unconfirmed() -> None:
    unknown_runner = {"kind": "test", "path": f"{SCRATCH_ROOT}/run-1/test_bug.rb"}
    outside = {"kind": "test", "path": "backend/test_elsewhere.py"}
    timed_out = {"kind": "command", "command": "sleep 999", "expect": {"exit_code": "nonzero"}}
    box = _AdvBox(**{"sleep 999": _res(1, "", timed_out=True)})
    blob = await _round(
        StubLlm([_answer(_finding(unknown_runner), _finding(outside), _finding(timed_out))]), box
    )

    assert blob["status"] == "done"
    assert [f["confirmed"] for f in blob["findings"]] == [False, False, False]
    assert not any("pytest" in c for c in box.commands)


async def test_long_repro_output_is_cut() -> None:
    box = _AdvBox(pytest=_res(1, "x" * 50_000))
    blob = await _round(StubLlm([_answer(_finding())]), box)
    assert len(blob["findings"][0]["output"]) < 2000


async def test_non_test_write_refused_and_scratch_removed() -> None:
    box = _AdvBox(pytest=_res(1, "failed"))
    llm = StubLlm(
        [
            _tool("write_test", path="backend/svc.py", content="print('pwned')"),
            _tool("write_test", path=f"{SCRATCH_ROOT}/run-1/test_ok.py", content="assert False"),
            _answer(_finding()),
        ]
    )
    blob = await _round(llm, box)

    assert "backend/svc.py" not in box.writes
    assert list(box.writes) == [f"{SCRATCH_ROOT}/run-1/test_ok.py"]
    refused = [m for m in llm.calls[1]["messages"] if m.get("role") == "tool"]
    assert refused[0]["content"].startswith("REFUSED")
    assert any("(거절)" in c for c in blob["checks"])
    # the harness deletes the scratch directory when it is done, and only then
    removals = [c for c in box.commands if "rm -rf" in c]
    assert removals and SCRATCH_ROOT in removals[0]
    assert box.commands.index(removals[0]) > max(
        i for i, c in enumerate(box.commands) if "pytest" in c
    )


async def test_a_stray_write_by_shell_is_undone() -> None:
    box = _AdvBox()
    box.status_out = " M backend/svc.py\n?? notes.txt"
    llm = StubLlm([_tool("shell_exec", command="echo x >> backend/svc.py"), _answer()])
    await _round(llm, box)

    assert any("checkout -- backend/svc.py" in c for c in box.commands)
    assert any("rm -rf -- notes.txt" in c for c in box.commands)


async def test_artifact_number_mismatch_is_confirmed() -> None:
    # the report says 42 rows; re-running its source command prints 40
    repro = {
        "kind": "command",
        "command": "wc -l < data.csv",
        "expect": {"not_contains": "42"},
    }
    box = _AdvBox(**{"wc -l < data.csv": _res(0, "40")})
    llm = StubLlm([_tool("grep", pattern="42 rows"), _answer(_finding(repro))])
    blob = await _round(llm, box, surface="artifact")

    assert blob["surface"] == "artifact"
    assert blob["findings"][0]["confirmed"] is True
    assert "source command" in llm.calls[0]["messages"][0]["content"]  # the artifact checklist

    agreeing = _AdvBox(**{"wc -l < data.csv": _res(0, "42")})
    again = await _round(StubLlm([_answer(_finding(repro))]), agreeing, surface="artifact")
    assert again["findings"][0]["confirmed"] is False


async def test_an_llm_failure_is_unavailable_not_an_error() -> None:
    blob = await _round(StubLlm([]), _AdvBox())  # exhausted → the stub raises
    assert blob["status"] == "unavailable"
    assert blob["findings"] == []


# -- inside verify ---------------------------------------------------------------------------


async def _verify(
    *, verifier: StubLlm | None, command: str = "true", box: _AdvBox | None = None
) -> Any:
    async with memory_session() as session:
        run = await _make_run(session)
        run.payload = {**run.payload, "intent_text": "ORIGINAL-REQUEST"}
        work_step, attempt = await _make_step_and_attempt(session, run)
        contract = VerificationContract(
            checks=(VerificationCheck(kind="command", command=command),)
        )
        svc = VerificationService(session=session, llm=StubLlm([_answer()]), verifier_llm=verifier)
        return await svc.verify(
            run=run,
            work_step=work_step,
            attempt=attempt,
            contract=contract,
            box=box or _AdvBox(pytest=_res(1, "failed")),
            written_paths=[],
            final_text="done",
            baseline="base123",
        )


async def test_confirmed_finding_does_not_change_passed() -> None:
    repro = {"kind": "command", "command": "probe", "expect": {"exit_code": "nonzero"}}
    verifier = StubLlm([_answer(_finding(repro))])
    vr = await _verify(verifier=verifier, box=_AdvBox(probe=_res(2, "bad")))

    assert vr.outcome is VerificationOutcome.PASSED
    assert vr.result["adversarial"]["confirmed"] == 1
    # the existing review is untouched — it still runs alongside
    assert "review" in vr.result


async def test_gate_failure_skips_adversarial() -> None:
    verifier = StubLlm([_answer(_finding())])
    box = _AdvBox(**{"false": _res(1, "red")})
    vr = await _verify(verifier=verifier, command="false", box=box)

    assert vr.outcome is VerificationOutcome.FAILED
    assert "adversarial" not in vr.result
    assert verifier.calls == []


async def test_without_a_verifier_session_nothing_runs() -> None:
    vr = await _verify(verifier=None)
    assert "adversarial" not in vr.result


def test_quality_counts_adversarial() -> None:
    def _round_with(*confirmed: bool) -> Any:
        findings = [{"claim": "c", "confirmed": c} for c in confirmed]
        return SimpleNamespace(result={"adversarial": {"status": "done", "findings": findings}})

    rounds = [
        SimpleNamespace(result={}),  # no adversarial run -> not counted
        _round_with(True, False, False),
        _round_with(),  # ran, nothing found
    ]
    assert _tally_adversarial(rounds) == (2, 1, 2)
    assert _tally_adversarial([]) == (0, 0, 0)


def test_card_shows_confirmed_first_and_unconfirmed_as_suspected() -> None:
    blob = {
        "status": "done",
        "findings": [
            {"severity": "low", "claim": "maybe", "confirmed": False},
            {"severity": "high", "claim": "proved", "file": "a.py", "line": 2, "confirmed": True},
        ],
    }
    status, confirmed, suspected = _adversarial_summary({"adversarial": blob})
    assert status == "done"
    assert [f.claim for f in confirmed] == ["proved"]
    assert [f.claim for f in suspected] == ["maybe"]
    assert _adversarial_summary({})[0] == "none"
    assert _adversarial_summary({"adversarial": {"status": "unavailable"}})[0] == "unavailable"
