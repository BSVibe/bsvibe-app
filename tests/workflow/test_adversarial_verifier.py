"""The adversarial verifier — through ``VerificationService.verify`` and the harness directly.

Only the LLM is a stub. The verdict on every finding is the HARNESS's (a repro's exit code /
output); the model's own opinion of its finding never counts. Stage 1 is report-only.
"""

from __future__ import annotations

import dataclasses
import json
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from backend.workflow.application import adversarial_verifier
from backend.workflow.application.adversarial_verifier import run_adversarial_round
from backend.workflow.application.agent_loop import LoopTurn
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
    return LoopTurn(content=json.dumps({"tool": name, "args": args}), tool_calls=())


def _res(code: int = 0, out: str = "", *, timed_out: bool = False) -> SandboxResult:
    return SandboxResult(exit_code=code, stdout=out, stderr="", timed_out=timed_out)


class _AdvBox(_GitBox):
    """A git box whose pytest / repro commands are scripted and whose commands are recorded."""

    def __init__(self, *, pytest: SandboxResult | None = None, **scripted: SandboxResult) -> None:
        super().__init__()
        self.pytest = pytest or _res(0, "1 passed")
        self.scripted = scripted
        self.commands: list[str] = []

    async def exec(self, command: str, **kwargs: Any) -> SandboxResult:
        self.commands.append(command)
        if "status --porcelain" in command or "tar --exclude" in command or "rm -rf" in command:
            return _res(0, "")  # fingerprint (unchanged) / the copy / its removal
        inner = re.sub(r"^cd \S+ && ", "", command)  # the copy wrapper
        if "pytest" in inner:
            return self.pytest
        bare = inner.removeprefix(CI_PREFIX)
        if bare in self.scripted:
            return self.scripted[bare]
        return await super().exec(command, **kwargs)


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
    blob = await _round(StubLlm([_tool("grep", pattern="x"), _answer(_finding())]), box)

    assert blob["status"] == "done"
    assert (blob["confirmed"], blob["unconfirmed"]) == (1, 0)
    assert blob["findings"][0]["confirmed"] is True
    assert "1 failed" in blob["findings"][0]["output"]


async def test_passing_repro_is_unconfirmed_even_if_the_model_says_confirmed() -> None:
    box = _AdvBox(pytest=_res(0, "1 passed"))
    blob = await _round(
        StubLlm([_tool("grep", pattern="x"), _answer(_finding(confirmed=True))]), box
    )

    assert (blob["confirmed"], blob["unconfirmed"]) == (0, 1)
    assert blob["findings"][0]["confirmed"] is False


async def test_a_repro_that_cannot_run_is_unconfirmed() -> None:
    unknown_runner = {"kind": "test", "path": f"{SCRATCH_ROOT}/run-1/test_bug.rb"}
    outside = {"kind": "test", "path": "backend/test_elsewhere.py"}
    timed_out = {"kind": "command", "command": "sleep 999", "expect": {"exit_code": "nonzero"}}
    box = _AdvBox(**{"sleep 999": _res(1, "", timed_out=True)})
    blob = await _round(
        StubLlm(
            [
                _tool("grep", pattern="x"),
                _answer(_finding(unknown_runner), _finding(outside), _finding(timed_out)),
            ]
        ),
        box,
    )

    assert blob["status"] == "done"
    assert [f["confirmed"] for f in blob["findings"]] == [False, False, False]
    assert not any("pytest" in c for c in box.commands)


async def test_long_repro_output_is_cut() -> None:
    box = _AdvBox(pytest=_res(1, "x" * 50_000))
    blob = await _round(StubLlm([_tool("grep", pattern="x"), _answer(_finding())]), box)
    assert len(blob["findings"][0]["output"]) < 2000


async def test_non_test_write_refused_and_copy_removed() -> None:
    box = _AdvBox(pytest=_res(1, "failed"))
    llm = StubLlm(
        [
            _tool("write_test", path="backend/svc.py", content="print('pwned')"),
            _tool("write_test", path=f"{SCRATCH_ROOT}/run-1/test_ok.py", content="assert False"),
            _answer(_finding()),
        ]
    )
    blob = await _round(llm, box)

    writes = [c for c in box.commands if "base64 -d" in c]
    assert len(writes) == 1 and f"{SCRATCH_ROOT}/run-1/test_ok.py" in writes[0]
    assert not any("backend/svc.py" in c for c in writes)
    refused = [
        m for m in llm.calls[1]["messages"] if str(m.get("content", "")).startswith("TOOL RESULT")
    ]
    assert "REFUSED" in refused[0]["content"]
    assert any("(거절)" in c for c in blob["checks"])
    # the harness deletes every copy (the verifier's, scratch dir included, and the repro's fresh
    # one); the verifier's copy goes last, after the repro has run
    removals = [i for i, c in enumerate(box.commands) if "rm -rf" in c]
    assert len(removals) == 2
    assert removals[0] > max(i for i, c in enumerate(box.commands) if "pytest" in c)
    # the repro file alone is carried into the fresh copy
    carries = [c for c in box.commands if " cp -- " in c]
    assert len(carries) == 1 and "test_bug.py" in carries[0]


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
    again = await _round(
        StubLlm([_tool("grep", pattern="x"), _answer(_finding(repro))]),
        agreeing,
        surface="artifact",
    )
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
        svc = VerificationService(
            session=session,
            llm=StubLlm([_tool("grep", pattern="x"), _answer()]),
            verifier_llm=verifier,
        )
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
    verifier = StubLlm([_tool("grep", pattern="x"), _answer(_finding(repro))])
    vr = await _verify(verifier=verifier, box=_AdvBox(probe=_res(2, "bad")))

    assert vr.outcome is VerificationOutcome.PASSED
    assert vr.result["adversarial"]["confirmed"] == 1
    # the existing review is untouched — it still runs alongside
    assert "review" in vr.result


async def test_gate_failure_skips_adversarial() -> None:
    verifier = StubLlm([_tool("grep", pattern="x"), _answer(_finding())])
    box = _AdvBox(**{"false": _res(1, "red")})
    vr = await _verify(verifier=verifier, command="false", box=box)

    assert vr.outcome is VerificationOutcome.FAILED
    assert "adversarial" not in vr.result
    assert verifier.calls == []


async def test_without_a_verifier_session_nothing_runs() -> None:
    vr = await _verify(verifier=None)
    assert "adversarial" not in vr.result


def test_quality_counts_adversarial_and_keeps_unavailable_apart() -> None:
    def _round_with(*confirmed: bool) -> Any:
        findings = [{"claim": "c", "confirmed": c} for c in confirmed]
        return SimpleNamespace(result={"adversarial": {"status": "done", "findings": findings}})

    rounds = [
        SimpleNamespace(result={}),  # no adversarial run -> not counted
        _round_with(True, False, False),
        _round_with(),  # ran, nothing found
        SimpleNamespace(result={"adversarial": {"status": "unavailable", "findings": []}}),
    ]
    # (runs, unavailable, confirmed, unconfirmed, no_probe): an unavailable round is NOT a run
    assert _tally_adversarial(rounds) == (2, 1, 1, 2, 0)
    assert _tally_adversarial([]) == (0, 0, 0, 0, 0)


def test_quality_counts_no_probe_apart_from_done_and_unavailable() -> None:
    def _r(status: str) -> Any:
        return SimpleNamespace(result={"adversarial": {"status": status, "findings": []}})

    rounds = [_r("done"), _r("no_probe"), _r("no_probe"), _r("unavailable")]
    assert _tally_adversarial(rounds) == (1, 1, 0, 0, 2)


async def test_no_tool_use_is_sent_back_once_then_recorded_as_no_probe() -> None:
    llm = StubLlm([_answer(), _answer()])
    blob = await _round(llm, _AdvBox())

    assert blob["status"] == "no_probe"
    assert blob["probes"] == 0
    assert len(llm.calls) == 2  # the nudge reached the model exactly once
    assert "at least once" in llm.calls[1]["messages"][-1]["content"]


async def test_probing_after_the_nudge_is_done() -> None:
    llm = StubLlm([_answer(), _tool("grep", pattern="x"), _answer()])
    blob = await _round(llm, _AdvBox())

    assert blob["status"] == "done"
    assert blob["probes"] >= 1


async def test_refused_write_test_after_nudge_is_still_no_probe() -> None:
    llm = StubLlm(
        [
            _answer(),
            _tool("write_test", path="backend/svc.py", content="x"),
            _answer(),
        ]
    )
    blob = await _round(llm, _AdvBox())

    assert blob["status"] == "no_probe"
    assert blob["probes"] == 0


async def test_probe_then_answer_is_done_without_a_nudge() -> None:
    llm = StubLlm([_tool("grep", pattern="x"), _answer()])
    blob = await _round(llm, _AdvBox())

    assert blob["status"] == "done"
    assert blob["probes"] == 1
    assert len(llm.calls) == 2


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


# -- isolation: a real filesystem, a real git --------------------------------------------------


class _LocalBox:
    """A sandbox that is just a directory on this machine: commands really run, really write."""

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace

    @property
    def workspace_mount(self) -> str:
        return str(self.workspace)

    async def exec(self, command: str, **kwargs: Any) -> SandboxResult:
        done = subprocess.run(  # noqa: S602, ASYNC221
            command, shell=True, cwd=self.workspace, capture_output=True, text=True, timeout=60
        )
        return SandboxResult(done.returncode, done.stdout, done.stderr, False)

    async def read_file(self, rel_path: str, max_bytes: int) -> bytes:
        return (self.workspace / rel_path).read_bytes()[:max_bytes]

    async def write_file(self, rel_path: str, content: bytes) -> None:
        (self.workspace / rel_path).write_bytes(content)

    async def list_dir(self, rel_path: str) -> list[str]:
        return []


@pytest.fixture(autouse=True)
def _git_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    for who in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{who}_NAME", "t")
        monkeypatch.setenv(f"GIT_{who}_EMAIL", "t@t")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(  # noqa: S603
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],  # noqa: S607
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def _real_tree(tmp_path: Path) -> Path:
    repo = tmp_path / "ws"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "src.py").write_text("def f():\n    return 1\n")
    (repo / "other.py").write_text("x = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    (repo / "src.py").write_text("def f():\n    return 2\n")
    _git(repo, "commit", "-qam", "change")
    (repo / "other.py").write_text("x = 99  # uncommitted\n")  # already-modified file
    (repo / "scratch_notes.txt").write_text("untracked\n")
    return repo


def _tree_state(repo: Path) -> tuple[str, str, str, str]:
    return (
        _git(repo, "rev-parse", "HEAD"),
        _git(repo, "status", "--porcelain"),
        _git(repo, "diff", "HEAD"),
        notes.read_text() if (notes := repo / "scratch_notes.txt").exists() else "<gone>",
    )


_HOSTILE = (
    "echo hacked >> src.py; echo hacked >> other.py; rm scratch_notes.txt; "
    "git add -A; git commit -qm evil; git reset -q --hard HEAD~1; git checkout -q -- .; "
    "git commit -q --allow-empty -m again"
)


async def _local_round(repo: Path, llm: StubLlm) -> dict[str, Any]:
    async def release() -> None:
        return None

    base = _git(repo, "rev-parse", "HEAD~1").strip()
    return await run_adversarial_round(
        llm=llm,
        run=_run(),
        box=_LocalBox(repo),  # type: ignore[arg-type]
        baseline=base,
        surface="code",
        release=release,
    )


async def test_hostile_shell_leaves_the_real_tree_untouched(tmp_path: Path) -> None:
    repo = _real_tree(tmp_path)
    before = _tree_state(repo)
    probe = {
        "kind": "command",
        "command": "grep -c uncommitted other.py",
        "expect": {"contains": "1"},
    }
    llm = StubLlm([_tool("shell_exec", command=_HOSTILE), _answer(_finding(probe))])

    blob = await _local_round(repo, llm)

    assert _tree_state(repo) == before  # HEAD, status, diff, untracked content: all identical
    assert blob["status"] == "done"
    # the repro ran against a copy of the VERIFIED state, uncommitted edit included
    assert blob["findings"][0]["confirmed"] is True
    assert not list(tmp_path.glob("**/.bsvibe_adversarial"))  # the scratch dir is gone
    assert not list(Path("/tmp").glob("bsvibe-adversarial-run-1-*"))  # noqa: S108 — and the copy


async def test_verifier_edits_never_reach_the_verdict(tmp_path: Path) -> None:
    """The verifier breaks production code in ITS copy, then submits a repro for the breakage.

    The repro runs in a fresh copy of the verified state, so the finding stays unconfirmed.
    Running repros in the verifier's own copy would turn this red."""
    repo = _real_tree(tmp_path)  # verified: f() returns 2
    repro = {
        "kind": "command",
        "command": "grep -q 'return 2' src.py",
        "expect": {"exit_code": "nonzero"},  # "f() no longer returns 2"
    }
    sabotage = "sed -i.bak 's/return 2/return 3/' src.py"
    llm = StubLlm([_tool("shell_exec", command=sabotage), _answer(_finding(repro))])

    blob = await _local_round(repo, llm)

    assert blob["status"] == "done"
    assert blob["findings"][0]["confirmed"] is False
    assert not list(Path("/tmp").glob("bsvibe-adversarial-run-1-*"))  # noqa: S108 — all removed


async def test_the_verifier_is_given_the_working_tree_diff(tmp_path: Path) -> None:
    repo = _real_tree(tmp_path)  # src.py committed after the baseline, other.py uncommitted
    llm = StubLlm([_tool("grep", pattern="x"), _answer()])
    await _local_round(repo, llm)
    user = next(m for m in llm.calls[0]["messages"] if m["role"] == "user")["content"]
    assert "x = 99  # uncommitted" in user  # `git diff baseline HEAD` would not have it
    assert "return 2" in user
    assert "scratch_notes.txt" in user  # untracked files are named


async def test_without_isolation_the_hostile_shell_would_be_caught(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """Pointing the copy at the real tree (no isolation) must turn the guard red."""
    repo = _real_tree(tmp_path)

    async def no_copy(box: Any, copy: Any) -> None:
        copy.root = str(repo)  # the verifier now works in the REAL tree

    async def no_cleanup(box: Any, copy: Any, budget: Any) -> None:
        return None  # the "copy" is the real tree: do not delete it

    monkeypatch.setattr(adversarial_verifier, "_make_copy", no_copy)
    monkeypatch.setattr(adversarial_verifier, "_cleanup", no_cleanup)
    before = _tree_state(repo)
    blob = await _local_round(repo, StubLlm([_tool("shell_exec", command=_HOSTILE), _answer()]))

    assert _tree_state(repo) == before  # the damage happened, then the harness restored it...
    assert blob["status"] == "unavailable"  # ...and discarded the result
    assert blob["reason"].startswith("harness_error")
    assert "tree_unrestored" not in blob
    assert blob["findings"] == []


async def test_an_absolute_path_escape_is_restored(tmp_path: Path) -> None:
    repo = _real_tree(tmp_path)
    before = _tree_state(repo)
    escape = (
        f"echo hacked >> {repo}/src.py; echo new > {repo}/planted.txt; "
        f"cd {repo} && git commit -qam evil && git reset -q --hard HEAD~1"
    )
    blob = await _local_round(repo, StubLlm([_tool("shell_exec", command=escape), _answer()]))

    assert _tree_state(repo) == before  # HEAD, status, diff, untracked: the pre-stage state
    assert not (repo / "planted.txt").exists()
    assert blob["status"] == "unavailable"
    assert blob["reason"].startswith("harness_error")


async def test_without_the_restore_the_escape_stays_and_the_round_fails(
    monkeypatch: Any, tmp_path: Path
) -> None:
    repo = _real_tree(tmp_path)

    async def no_restore(box: Any, path: str) -> None:
        return None

    monkeypatch.setattr(adversarial_verifier, "_restore", no_restore)
    escape = f"echo hacked >> {repo}/src.py"
    blob = await _local_round(repo, StubLlm([_tool("shell_exec", command=escape), _answer()]))

    assert "hacked" in (repo / "src.py").read_text()  # nothing put it back...
    assert blob["tree_unrestored"] is True  # ...so the round is failed, not passed on
    assert blob["reason"].startswith("harness_error")


async def test_an_unrestorable_tree_fails_the_round(monkeypatch: Any) -> None:
    from backend.workflow.application import verification_service

    async def polluted(**kwargs: Any) -> dict[str, Any]:
        return adversarial_verifier._unrestored("code", [], "복원 후 지문이 다르다")

    monkeypatch.setattr(verification_service, "run_adversarial_round", polluted)
    vr = await _verify(verifier=StubLlm([_tool("grep", pattern="x"), _answer()]))

    assert vr.outcome is VerificationOutcome.FAILED  # a polluted tree never flows on
    assert vr.result["adversarial"]["reason"].startswith("harness_error")


async def test_the_token_cap_is_a_real_stop(monkeypatch: Any) -> None:
    monkeypatch.setattr(adversarial_verifier, "_TOKEN_CAP", 10)
    heavy = dataclasses.replace(_tool("shell_exec", command="probe"), usage_prompt_tokens=50)
    llm = StubLlm([heavy, _tool("shell_exec", command="never"), _answer(_finding())])
    box = _AdvBox(pytest=_res(1, "failed"))

    blob = await _round(llm, box)

    assert blob["status"] == "unavailable"
    assert "토큰" in blob["reason"]
    assert blob["findings"] == []
    assert len(llm.calls) == 1  # the loop ended at once
    assert not any(c.endswith("probe") for c in box.commands)  # and its tool call never ran


async def test_when_the_time_budget_runs_out_remaining_repros_are_unconfirmed(
    monkeypatch: Any,
) -> None:
    box = _AdvBox(pytest=_res(1, "failed"))
    first_done = {"n": 0}
    real_judge = adversarial_verifier._judge

    async def spend_budget(box: Any, copy: Any, scratch: str, finding: Any, budget: Any) -> Any:
        out = await real_judge(box, copy, scratch, finding, budget)
        first_done["n"] += 1
        monkeypatch.setattr(budget, "remaining", lambda: 0.0)  # the clock runs out after #1
        return out

    monkeypatch.setattr(adversarial_verifier, "_judge", spend_budget)
    blob = await _round(
        StubLlm([_tool("grep", pattern="x"), _answer(_finding(), _finding(), _finding())]), box
    )

    assert [f["confirmed"] for f in blob["findings"]] == [True, False, False]
    assert sum("pytest" in c for c in box.commands) == 1  # #2 and #3 never ran
