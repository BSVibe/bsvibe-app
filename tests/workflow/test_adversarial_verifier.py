"""The adversarial verifier — through ``VerificationService.verify`` and the harness directly.

Only the LLM is a stub. The verdict on every finding is the HARNESS's (a repro's exit code /
output); the model's own opinion of its finding never counts. Stage 1 is report-only.
"""

from __future__ import annotations

import dataclasses
import json
import re
import shlex
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
    MAX_TEST_CHARS,
    REPRO_RUN_PREFIX,
    SCRATCH_ROOT,
    command_did_not_run,
    judge_command,
    judge_test,
    parse_findings,
    runner_is_missing,
    write_refusal,
)
from backend.workflow.domain.declared_checks import CI_PREFIX
from backend.workflow.domain.verifier_contract import VerificationCheck, VerificationContract
from backend.workflow.infrastructure.db import VerificationOutcome
from backend.workflow.infrastructure.sandbox import SandboxResult
from tests._support import memory_session
from tests.execution.test_verification_service import (
    StubLlm as _ScriptedLlm,
)
from tests.execution.test_verification_service import (
    _make_run,
    _make_step_and_attempt,
)
from tests.workflow.test_artifact_review import _GitBox

_NO_CONFLICT = LoopTurn(content='{"conflict": false, "why": "unrelated"}', tool_calls=())
_CONFLICT = LoopTurn(content='{"conflict": true, "why": "the request asked for it"}', tool_calls=())


class StubLlm(_ScriptedLlm):
    """Scripted turns; once they run out, the intent judge (the only call left) finds no conflict."""

    async def complete(self, *, messages: list[dict[str, Any]], tools: Any) -> LoopTurn:
        if not self._turns:
            self._turns.append(_NO_CONFLICT)
        return await super().complete(messages=messages, tools=tools)


_UV_SH = f"{REPRO_RUN_PREFIX}sh -c "
_TEST_REPRO = {"kind": "test", "path": f"{SCRATCH_ROOT}/run-1/test_bug.py"}


def _finding(repro: dict[str, Any] | None = None, **extra: Any) -> dict[str, Any]:
    return {
        "severity": "high",
        "claim": "cleanup is never wired",
        "file": "backend/svc.py",
        "line": 3,
        "repro": repro or _TEST_REPRO,
        "expected_from": {"kind": "unchanged_contract", "location": "backend/svc.py:3"},
        **extra,
    }


def _answer(*findings: dict[str, Any]) -> LoopTurn:
    return LoopTurn(content=json.dumps({"findings": list(findings)}), tool_calls=())


def _tool(name: str, **args: Any) -> LoopTurn:
    return LoopTurn(content=json.dumps({"tool": name, "args": args}), tool_calls=())


def _res(code: int = 0, out: str = "", *, timed_out: bool = False) -> SandboxResult:
    return SandboxResult(exit_code=code, stdout=out, stderr="", timed_out=timed_out)


class _AdvBox(_GitBox):
    """A git box whose pytest / repro commands are scripted and whose commands are recorded.

    ``pytest`` / ``scripted`` answer the repro on the changed tree; ``pytest_before`` /
    ``scripted_before`` answer the same repro on the baseline copy (the ``-before-`` directory).
    A repro with no 'before' answer shows nothing before the change (pytest passes, the command
    exits 0 with no output)."""

    def __init__(
        self,
        *,
        pytest: SandboxResult | None = None,
        pytest_before: SandboxResult | None = None,
        scripted_before: dict[str, SandboxResult] | None = None,
        **scripted: SandboxResult,
    ) -> None:
        super().__init__()
        self.pytest = pytest or _res(0, "1 passed")
        self.pytest_before = pytest_before or _res(0, "1 passed")
        self.scripted = scripted
        self.scripted_before = scripted_before or {}
        self.commands: list[str] = []
        #: True: a repro file exists only if the session wrote it (``write_test`` / ``content``).
        self.written_only = False
        self.written: list[str] = []

    async def exec(self, command: str, **kwargs: Any) -> SandboxResult:
        self.commands.append(command)
        inner0 = re.sub(r"^cd \S+ && ", "", command)
        if "base64 -d >" in inner0:  # copy.write_file
            self.written.append(shlex.split(inner0.rsplit("base64 -d >", 1)[1])[0])
            return _res(0, "")
        if inner0.startswith("test -f ") and "-before-" not in command:
            path = shlex.split(inner0)[2]
            return _res(0 if (not self.written_only or path in self.written) else 1, "")
        if (
            "status --porcelain" in command
            or "tar --exclude" in command
            or "rm -rf" in command
            or "git archive" in command  # the baseline copy's rewind
        ):
            return _res(0, "")  # fingerprint (unchanged) / the copy / its removal
        before = "-before-" in command
        inner = re.sub(r"^cd \S+ && ", "", command)  # the copy wrapper
        if "pytest" in inner:
            return self.pytest_before if before else self.pytest
        bare = inner.removeprefix(CI_PREFIX)
        if bare.startswith(_UV_SH):  # a command repro, as the harness wraps it
            bare = shlex.split(bare.removeprefix(_UV_SH))[0]
        table = self.scripted_before if before else self.scripted
        if bare in table:
            return table[bare]
        if before and bare in self.scripted:
            return _res(0, "")
        return await super().exec(command, **kwargs)


def _run(payload: dict[str, Any] | None = None) -> Any:
    return SimpleNamespace(
        id="run-1",
        payload=payload or {"intent_text": "do it"},
        usage_prompt_tokens=0,
        usage_completion_tokens=0,
    )


async def _round(
    llm: StubLlm, box: _AdvBox, surface: str = "code", run: Any = None
) -> dict[str, Any]:
    async def release() -> None:
        return None

    return await run_adversarial_round(
        llm=llm, run=run or _run(), box=box, baseline="base123", surface=surface, release=release
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
    # the harness deletes every copy (the verifier's, scratch dir included, the repro's fresh one
    # and the baseline one); the verifier's copy goes last, after the repro has run
    removals = [i for i, c in enumerate(box.commands) if "rm -rf" in c]
    assert len(removals) == 3
    assert removals[0] > max(i for i, c in enumerate(box.commands) if "pytest" in c)
    # the repro file alone is carried into the fresh copy and the baseline copy
    carries = [c for c in box.commands if " cp -- " in c]
    assert len(carries) == 2 and all("test_bug.py" in c for c in carries)


async def test_artifact_number_mismatch_is_confirmed() -> None:
    # the report says 42 rows; re-running its source command prints 40
    repro = {
        "kind": "command",
        "command": "wc -l < data.csv",
        "expect": {"not_contains": "42"},
    }
    box = _AdvBox(
        scripted_before={"wc -l < data.csv": _res(0, "42")},  # the report was right before
        **{"wc -l < data.csv": _res(0, "40")},
    )
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


async def test_first_answer_survives_two_malformed_replies_after_the_nudge() -> None:
    junk = LoopTurn(content="not json", tool_calls=())
    llm = StubLlm([_answer(_finding()), junk, junk])
    blob = await _round(llm, _AdvBox(pytest=_res(1, "1 failed")))

    assert blob["status"] == "no_probe"
    assert blob["probes"] == 0
    assert len(blob["findings"]) == 1  # the first answer's finding is kept and judged
    assert blob["findings"][0]["confirmed"] is True


def test_no_probe_rounds_keep_their_confirmed_findings_on_card_and_tally() -> None:
    adv = {
        "status": "no_probe",
        "findings": [
            {"severity": "high", "claim": "proved", "confirmed": True},
            {"severity": "low", "claim": "maybe", "confirmed": False},
        ],
    }
    status, confirmed, suspected = _adversarial_summary({"adversarial": adv})
    assert status == "no_probe"
    assert [f.claim for f in confirmed] == ["proved"]
    assert [f.claim for f in suspected] == ["maybe"]
    # not a "done" run, but its harness-reproduced findings are counted
    assert _tally_adversarial([SimpleNamespace(result={"adversarial": adv})]) == (0, 0, 1, 1, 1)


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


def _big_tool(prompt: int) -> LoopTurn:
    turn = _tool("shell_exec", command="probe")
    return dataclasses.replace(turn, usage_prompt_tokens=prompt, usage_completion_tokens=10)


def _big_answer(prompt: int, *findings: dict[str, Any]) -> LoopTurn:
    return dataclasses.replace(
        _answer(*findings), usage_prompt_tokens=prompt, usage_completion_tokens=10
    )


async def test_token_cap_asks_for_the_final_json_once(monkeypatch: Any) -> None:
    monkeypatch.setattr(adversarial_verifier, "_TOKEN_CAP", 150_000)
    monkeypatch.setattr(adversarial_verifier, "_FINAL_MARGIN", 5_000)
    llm = StubLlm([_big_tool(30_000)] * 3 + [_big_answer(30_000, _finding())])
    box = _AdvBox(pytest=_res(1, "failed"))

    blob = await _round(llm, box)

    final_asks = [
        c
        for c in llm.calls
        if "Give the final JSON now" in str(c["messages"][-1].get("content", ""))
    ]
    assert len(final_asks) == 1
    assert blob["status"] == "done"
    assert blob["budget_stopped"] is True
    assert len(blob["findings"]) == 1
    session_calls = len(llm.calls) - 1  # the last call is the separate intent judge
    assert session_calls * 30_010 <= 150_000  # the whole session stays within the cap


async def test_token_cap_malformed_final_answer_is_unavailable(monkeypatch: Any) -> None:
    monkeypatch.setattr(adversarial_verifier, "_TOKEN_CAP", 150_000)
    monkeypatch.setattr(adversarial_verifier, "_FINAL_MARGIN", 5_000)
    junk = dataclasses.replace(
        LoopTurn(content="not json", tool_calls=()), usage_prompt_tokens=30_000
    )
    llm = StubLlm([_big_tool(30_000)] * 3 + [junk, junk])

    blob = await _round(llm, _AdvBox())

    assert blob["status"] == "unavailable"
    assert "토큰" in blob["reason"]
    assert len(llm.calls) <= 4


async def test_when_the_time_budget_runs_out_remaining_repros_are_unconfirmed(
    monkeypatch: Any,
) -> None:
    box = _AdvBox(pytest=_res(1, "failed"))
    first_done = {"n": 0}
    real_judge = adversarial_verifier._judge

    async def spend_budget(
        box: Any, copy: Any, scratch: str, finding: Any, budget: Any, **kw: Any
    ) -> Any:
        out = await real_judge(box, copy, scratch, finding, budget, **kw)
        first_done["n"] += 1
        monkeypatch.setattr(budget, "remaining", lambda: 0.0)  # the clock runs out after #1
        return out

    monkeypatch.setattr(adversarial_verifier, "_judge", spend_budget)
    blob = await _round(
        StubLlm([_tool("grep", pattern="x"), _answer(_finding(), _finding(), _finding())]), box
    )

    assert [f["confirmed"] for f in blob["findings"]] == [True, False, False]
    # #1 ran twice (changed tree + baseline control); #2 and #3 never ran
    assert sum("pytest" in c for c in box.commands) == 2


# -- baseline control: a repro that also holds BEFORE the change proves nothing ---------------


def _cmd_repro(command: str, **expect: Any) -> dict[str, Any]:
    return {"kind": "command", "command": command, "expect": expect}


async def _one(box: _AdvBox, repro: dict[str, Any]) -> dict[str, Any]:
    blob = await _round(StubLlm([_tool("grep", pattern="x"), _answer(_finding(repro))]), box)
    assert blob["status"] == "done"
    return blob["findings"][0]


async def test_grep_repro_that_also_holds_before_the_change_is_not_confirmed() -> None:
    probe = "grep -rl adversarial_unavailable ."
    repro = _cmd_repro(probe, contains="MISSING:")
    out = _res(0, "MISSING:backend/x.py")
    box = _AdvBox(scripted_before={probe: out}, **{probe: out})  # same output both sides

    finding = await _one(box, repro)

    assert finding["confirmed"] is False
    assert "변경 전에도 같은 결과" in finding["reason"]


async def test_command_that_matches_only_after_the_change_is_confirmed() -> None:
    probe = "check"
    box = _AdvBox(scripted_before={probe: _res(0, "fine")}, **{probe: _res(0, "MISSING:x")})
    finding = await _one(box, _cmd_repro(probe, contains="MISSING:"))
    assert finding["confirmed"] is True
    assert "변경 후에서만" in finding["reason"]


async def test_test_repro_failing_on_both_sides_is_not_confirmed() -> None:
    box = _AdvBox(pytest=_res(1, "1 failed"), pytest_before=_res(1, "1 failed"))
    finding = await _one(box, _TEST_REPRO)
    assert finding["confirmed"] is False
    assert "변경 전에도 같은 결과" in finding["reason"]


async def test_test_repro_failing_only_after_is_confirmed() -> None:
    box = _AdvBox(pytest=_res(1, "1 failed"), pytest_before=_res(0, "1 passed"))
    finding = await _one(box, _TEST_REPRO)
    assert finding["confirmed"] is True
    assert "변경 전에는 통과" in finding["reason"]


async def test_test_repro_that_cannot_be_collected_before_is_confirmed_with_the_reason() -> None:
    # the test imports what the change adds: before the change it cannot even be collected
    box = _AdvBox(pytest=_res(1, "1 failed"), pytest_before=_res(2, "ImportError"))
    finding = await _one(box, _TEST_REPRO)
    assert finding["confirmed"] is True
    assert "변경 전 실행 불가" in finding["reason"]
    assert finding["baseline_not_run"] is True


async def test_command_repro_that_cannot_run_before_is_never_confirmed() -> None:
    probe = "check"
    before = _res(1, "ModuleNotFoundError: No module named 'new_thing'")
    box = _AdvBox(scripted_before={probe: before}, **{probe: _res(0, "MISSING:x")})
    finding = await _one(box, _cmd_repro(probe, contains="MISSING:"))
    assert finding["confirmed"] is False
    assert "변경 전 실행 불가" in finding["reason"]


async def test_a_baseline_run_that_times_out_never_confirms() -> None:
    hung = _res(1, "", timed_out=True)
    test_box = _AdvBox(pytest=_res(1, "1 failed"), pytest_before=hung)
    test_finding = await _one(test_box, _TEST_REPRO)
    cmd_box = _AdvBox(scripted_before={"check": hung}, **{"check": _res(0, "MISSING:x")})
    cmd_finding = await _one(cmd_box, _cmd_repro("check", contains="MISSING:"))

    for finding in (test_finding, cmd_finding):
        assert finding["confirmed"] is False
        assert finding["reason"] == "변경 전 대조 시간 초과"


async def test_a_collection_error_is_not_run_rather_than_wrong() -> None:
    box = _AdvBox(pytest=_res(2, "ERROR collecting: ModuleNotFoundError: structlog"))
    finding = await _one(box, _TEST_REPRO)
    assert finding["confirmed"] is False
    assert finding["not_run"] is True
    assert finding["reason"].startswith("재현이 실행되지 않음")
    # no baseline control is spent on a repro that never ran
    assert not any("-before-" in c and "pytest" in c for c in box.commands)

    missing = "python -c 'import backend.x'"
    err = _res(1, "ModuleNotFoundError: No module named 'structlog'")
    cmd = await _one(_AdvBox(**{missing: err}), _cmd_repro(missing, exit_code="nonzero"))
    assert cmd["confirmed"] is False  # exit 1 matches `nonzero`, but nothing ran
    assert cmd["not_run"] is True
    assert cmd["reason"].startswith("재현이 실행되지 않음")


_NO_PYTEST = _res(1, "/tmp/x/.venv/bin/python: No module named pytest")


async def test_missing_test_runner_is_not_run_whatever_the_exit_code() -> None:
    box = _AdvBox(pytest=_NO_PYTEST, pytest_before=_NO_PYTEST)
    finding = await _one(box, _TEST_REPRO)
    assert finding["confirmed"] is False
    assert finding["not_run"] is True
    assert finding["reason"].startswith("재현이 실행되지 않음")
    assert "변경 전에도" not in finding["reason"]


async def test_real_failure_after_and_missing_runner_before_is_not_the_same_result() -> None:
    box = _AdvBox(pytest=_res(1, "assert 1 == 2\n1 failed"), pytest_before=_NO_PYTEST)
    finding = await _one(box, _TEST_REPRO)
    assert finding["confirmed"] is False
    assert finding["baseline_not_run"] is True
    assert "변경 전에도 같은 결과" not in finding["reason"]
    assert "변경 전 실행 불가" in finding["reason"]


async def test_assert_message_mentioning_missing_pytest_is_still_confirmed() -> None:
    quoted = _res(
        1,
        "=== test session starts ===\ncollected 1 item\n"
        "E   AssertionError: /x/python: No module named pytest\n"
        "=== 1 failed in 0.1s ===",
    )
    box = _AdvBox(pytest=quoted, pytest_before=_res(0, "1 passed"))
    finding = await _one(box, _TEST_REPRO)
    assert finding["confirmed"] is True
    assert not finding.get("not_run")

    # even without a run trace, a quoted message is not a runner error: whole lines only
    bare = _res(1, "E   assert 'x/python: No module named pytest' in out")
    again = await _one(_AdvBox(pytest=bare, pytest_before=_res(0, "1 passed")), _TEST_REPRO)
    assert again["confirmed"] is True

    assert runner_is_missing("error: Failed to spawn: `pytest`")
    assert runner_is_missing("sh: pytest: not found")
    assert runner_is_missing("/tmp/x/.venv/bin/python: No module named pytest")


async def test_real_failure_before_and_after_is_still_the_same_result() -> None:
    failed = _res(1, "assert 1 == 2\n1 failed")
    finding = await _one(_AdvBox(pytest=failed, pytest_before=failed), _TEST_REPRO)
    assert finding["confirmed"] is False
    assert finding["reason"] == "변경 전에도 같은 결과"


async def test_repros_run_with_the_gates_uv_run_prefix() -> None:
    probe = "python -c 'import backend.workflow.application.safe_mode_ledger'"
    box = _AdvBox(pytest=_res(1, "1 failed"), **{probe: _res(0, "")})
    await _round(
        StubLlm(
            [
                _tool("grep", pattern="x"),
                _answer(_finding(), _finding(_cmd_repro(probe, exit_code=0))),
            ]
        ),
        box,
    )
    repro_runs = [c for c in box.commands if "pytest" in c or " sh -c " in c]
    assert repro_runs, "no repro ran"
    for command in repro_runs:
        inner = re.sub(r"^cd \S+ && ", "", command)
        assert inner.startswith("uv run "), inner  # never a bare `python`
    assert any(f"{REPRO_RUN_PREFIX}python -m pytest" in c for c in repro_runs)
    assert any(c.endswith(shlex.quote(probe)) for c in repro_runs)


def test_the_verifier_is_told_where_repros_run() -> None:
    from backend.workflow.domain.adversarial import SYSTEM_PROMPT

    assert "uv run" in SYSTEM_PROMPT
    assert "BEFORE the change" in SYSTEM_PROMPT


def test_the_card_carries_why_a_suspected_finding_is_not_confirmed() -> None:
    blob = {
        "status": "done",
        "findings": [
            {"claim": "ran?", "confirmed": False, "reason": "재현이 실행되지 않음 (exit 2)"},
            {"claim": "proved", "confirmed": True, "reason": "x"},
        ],
    }
    _, confirmed, suspected = _adversarial_summary({"adversarial": blob})
    assert [f.reason for f in suspected] == ["재현이 실행되지 않음 (exit 2)"]
    assert [f.reason for f in confirmed] == [None]


# -- the baseline copy, on a real repository ------------------------------------------------------


async def test_baseline_copy_undoes_a_rename_and_additions(tmp_path: Path) -> None:
    repo = tmp_path / "ws"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "a.py").write_text("A = 1\n")
    (repo / "keep.py").write_text("K = 1\n")
    (repo / "gone.py").write_text("G = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    base = _git(repo, "rev-parse", "HEAD").strip()
    _git(repo, "mv", "a.py", "b.py")  # the change: a rename ...
    (repo / "keep.py").write_text("K = 2\n")  # ... an edit ...
    _git(repo, "rm", "-q", "gone.py")  # ... a deletion ...
    (repo / "new.py").write_text("N = 1\n")  # ... and new files, tracked and not
    _git(repo, "add", "new.py")
    (repo / "loose.py").write_text("L = 1\n")
    box = _LocalBox(repo)
    copy = adversarial_verifier._Copy(box, str(tmp_path / "copy"))  # type: ignore[arg-type]
    before = _tree_state(repo)

    await adversarial_verifier._make_copy(box, copy)  # type: ignore[arg-type]
    assert (tmp_path / "copy" / "b.py").exists()  # the verified state has the new path
    await adversarial_verifier._to_baseline(box, copy, base)  # type: ignore[arg-type]

    root = tmp_path / "copy"
    assert not (root / "b.py").exists()  # the renamed file's NEW path is gone
    assert (root / "a.py").read_text() == "A = 1\n"  # and the old one is back
    assert (root / "keep.py").read_text() == "K = 1\n"
    assert (root / "gone.py").exists()
    assert not (root / "new.py").exists()
    assert not (root / "loose.py").exists()
    assert _tree_state(repo) == before  # the real tree was only read


async def test_a_grep_repro_that_holds_on_both_trees_is_unconfirmed_for_real(
    tmp_path: Path,
) -> None:
    repo = _real_tree(tmp_path)  # "def f" exists both before and after the change
    probe = _cmd_repro("grep -c 'def f' src.py", contains="1")
    llm = StubLlm([_tool("grep", pattern="x"), _answer(_finding(probe))])

    blob = await _local_round(repo, llm)

    assert blob["findings"][0]["confirmed"] is False
    assert "변경 전에도 같은 결과" in blob["findings"][0]["reason"]
    assert not list(Path("/tmp").glob("bsvibe-adversarial-run-1-*"))  # noqa: S108 — all removed


# -- expected errors, summary reasons, baseline failures ----------------------------------------


async def test_expected_import_error_confirms_only_after_the_change() -> None:
    probe = "python -c 'import backend.x'"
    err = _res(1, "ModuleNotFoundError: No module named 'dep'")
    repro = _cmd_repro(probe, exit_code="nonzero", contains="No module named")

    after_only = await _one(_AdvBox(scripted_before={probe: _res(0, "")}, **{probe: err}), repro)
    assert after_only["confirmed"] is True
    assert not after_only.get("not_run")

    both = await _one(_AdvBox(scripted_before={probe: err}, **{probe: err}), repro)
    assert both["confirmed"] is False
    assert "변경 전에도 같은 결과" in both["reason"]


async def test_expected_exit_127_follows_the_same_rule() -> None:
    probe = "./bin/tool"
    gone = _res(127, "sh: ./bin/tool: not found")
    repro = _cmd_repro(probe, exit_code=127)
    ok = await _one(_AdvBox(scripted_before={probe: _res(0, "")}, **{probe: gone}), repro)
    assert ok["confirmed"] is True
    both = await _one(_AdvBox(scripted_before={probe: gone}, **{probe: gone}), repro)
    assert both["confirmed"] is False


async def test_unexpected_module_not_found_is_still_not_run() -> None:
    probe = "python -c 'import backend.x'"
    err = _res(1, "ModuleNotFoundError: No module named 'dep'")
    finding = await _one(_AdvBox(**{probe: err}), _cmd_repro(probe, exit_code="nonzero"))
    assert finding["confirmed"] is False
    assert finding["not_run"] is True


def test_summary_carries_baseline_not_run_reason() -> None:
    blob = {
        "status": "done",
        "findings": [
            {"severity": "high", "claim": "a", "confirmed": True, "reason": "ok"},
            {
                "severity": "high",
                "claim": "b",
                "confirmed": True,
                "reason": "변경 전 실행 불가",
                "baseline_not_run": True,
            },
            {"severity": "low", "claim": "c", "confirmed": False, "reason": "x", "not_run": True},
        ],
    }
    _, confirmed, suspected = _adversarial_summary({"adversarial": blob})
    by_claim = {f.claim: f for f in confirmed}
    assert by_claim["b"].reason == "변경 전 실행 불가"
    assert by_claim["b"].baseline_not_run is True
    assert by_claim["a"].reason is None
    assert suspected[0].not_run is True


async def test_baseline_with_missing_ref_raises(tmp_path: Path) -> None:
    repo = tmp_path / "ws"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "a.py").write_text("A = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    (repo / "added.py").write_text("N = 1\n")
    box = _LocalBox(repo)
    copy = adversarial_verifier._Copy(box, str(tmp_path / "copy"))  # type: ignore[arg-type]
    await adversarial_verifier._make_copy(box, copy)  # type: ignore[arg-type]

    with pytest.raises(RuntimeError):
        await adversarial_verifier._to_baseline(box, copy, "no-such-ref")  # type: ignore[arg-type]


async def test_a_repro_whose_baseline_cannot_be_built_is_unconfirmed(monkeypatch: Any) -> None:
    async def boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("bad ref")

    monkeypatch.setattr(adversarial_verifier, "_to_baseline", boom)
    probe = "check"
    box = _AdvBox(**{probe: _res(0, "MISSING:x")})
    finding = await _one(box, _cmd_repro(probe, contains="MISSING:"))
    assert finding["confirmed"] is False
    assert "변경 전 대조를 하지 못했다" in finding["reason"]


def test_repro_prefix_runs_without_a_lockfile(tmp_path: Path) -> None:
    """No ``uv.lock`` → ``--frozen`` would exit 2 for every repro; the prefix must not use it."""
    assert "--frozen" not in REPRO_RUN_PREFIX
    assert "--all-extras" in REPRO_RUN_PREFIX
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "lockless"\nversion = "0"\nrequires-python = ">=3.9"\n'
        "dependencies = []\n[tool.uv]\npackage = false\n"
    )
    done = subprocess.run(  # noqa: S602 — fixed test command
        f"{REPRO_RUN_PREFIX}python -c 'print(1)'",
        shell=True,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "1"


# -- the basis of the expectation: what the repro calls 'correct' must come from somewhere -----

_OLD_BEHAVIOUR = {
    "kind": "command",
    "command": "probe",
    "expect": {"exit_code": 0, "contains": "OLD"},
}


def _intent_run(request: str) -> Any:
    return _run({"intent_text": request})


async def _basis_round(
    finding: dict[str, Any], box: _AdvBox, *turns: LoopTurn, request: str = "do it"
) -> tuple[dict[str, Any], StubLlm, Any]:
    llm = StubLlm([_tool("grep", pattern="x"), _answer(finding), *turns])
    run = _intent_run(request)
    blob = await _round(llm, box, run=run)
    assert blob["status"] == "done"
    return blob["findings"][0], llm, run


def _only_after_box() -> _AdvBox:
    # the repro holds after the change and not before: the baseline control alone confirms it
    return _AdvBox(scripted_before={"probe": _res(0, "NEW")}, probe=_res(0, "OLD"))


async def test_intended_behaviour_change_is_not_confirmed_as_regression() -> None:
    request = "3. 재현이 import 오류로 끝나면 'unconfirmed: 재현이 실행되지 않음'으로 표시해"
    finding = _finding(
        _OLD_BEHAVIOUR,
        expected_from={"kind": "unchanged_contract", "location": "backend/x.py:211"},
    )
    result, llm, _ = await _basis_round(finding, _only_after_box(), _CONFLICT, request=request)

    assert result["confirmed"] is False
    assert "요청된 변경과 충돌" in result["reason"]
    assert len(llm.calls) == 3  # the session (2 turns) + ONE separate intent call
    assert "REQUEST:" in llm.calls[-1]["messages"][-1]["content"]
    assert request in llm.calls[-1]["messages"][-1]["content"]


async def test_the_same_repro_without_the_intent_check_would_be_confirmed() -> None:
    finding = _finding(
        _OLD_BEHAVIOUR,
        expected_from={"kind": "unchanged_contract", "location": "backend/x.py:211"},
    )
    result, _, _ = await _basis_round(finding, _only_after_box(), _NO_CONFLICT)
    assert result["confirmed"] is True


async def test_request_quote_not_in_request_is_unconfirmed() -> None:
    finding = _finding(
        _OLD_BEHAVIOUR,
        expected_from={
            "kind": "request",
            "quote": "the request never said anything like this sentence",
        },
    )
    result, llm, _ = await _basis_round(finding, _only_after_box())
    assert result["confirmed"] is False
    assert "기대 근거 불일치" in result["reason"]
    assert len(llm.calls) == 2  # no intent call for a failed mechanical check


async def test_request_quote_matches_after_whitespace_normalising_and_ledger_items() -> None:
    run = _run(
        {
            "intent_text": "first   line\nsecond line",
            "requirements": [{"id": "R7", "text": "must mark   it  not_run"}],
        }
    )
    for quote, req_id in (("first line second line", ""), ("must mark it not_run", "R7")):
        finding = _finding(
            _OLD_BEHAVIOUR, expected_from={"kind": "request", "quote": quote, "req_id": req_id}
        )
        llm = StubLlm([_tool("grep", pattern="x"), _answer(finding)])
        blob = await _round(llm, _only_after_box(), run=run)
        assert blob["findings"][0]["confirmed"] is True, quote


async def test_finding_without_expected_from_is_unconfirmed() -> None:
    finding = _finding(_OLD_BEHAVIOUR)
    del finding["expected_from"]
    result, _, _ = await _basis_round(finding, _only_after_box())
    assert result["confirmed"] is False
    assert result["reason"] == "기대 근거 없음"


async def test_existing_test_passing_before_failing_after_stays_confirmed() -> None:
    ef = {"kind": "existing_test", "test": "tests/test_old.py::test_keeps"}
    box = _AdvBox(pytest=_res(1, "1 failed"), pytest_before=_res(0, "1 passed"))
    # the repro test fails only after; the named test passes before and fails after
    result, _, _ = await _basis_round(_finding(expected_from=ef), box)
    assert result["confirmed"] is True


async def test_existing_test_that_does_not_pass_before_is_unconfirmed() -> None:
    ef = {"kind": "existing_test", "test": "tests/test_old.py::test_keeps"}

    class _Box(_AdvBox):
        async def exec(self, command: str, **kwargs: Any) -> SandboxResult:
            if "-before-" in command and "test_old.py::test_keeps" in command:
                return _res(1, "1 failed")
            return await super().exec(command, **kwargs)

    box = _Box(pytest=_res(1, "1 failed"), pytest_before=_res(0, "1 passed"))
    result, _, _ = await _basis_round(_finding(expected_from=ef), box)
    assert result["confirmed"] is False
    assert "기대 근거 불일치" in result["reason"]


async def test_existing_test_that_still_passes_after_is_unconfirmed() -> None:
    ef = {"kind": "existing_test", "test": "tests/test_old.py::test_keeps"}

    class _Box(_AdvBox):
        async def exec(self, command: str, **kwargs: Any) -> SandboxResult:
            if "-before-" not in command and "test_old.py::test_keeps" in command:
                return _res(0, "1 passed")
            return await super().exec(command, **kwargs)

    box = _Box(pytest=_res(1, "1 failed"), pytest_before=_res(0, "1 passed"))
    result, _, _ = await _basis_round(_finding(expected_from=ef), box)
    assert result["confirmed"] is False
    assert "기대 근거 불일치" in result["reason"]


async def test_unchanged_contract_needs_a_file_line_location() -> None:
    finding = _finding(
        _OLD_BEHAVIOUR, expected_from={"kind": "unchanged_contract", "location": "somewhere"}
    )
    result, _, _ = await _basis_round(finding, _only_after_box())
    assert result["confirmed"] is False
    assert "기대 근거 불일치" in result["reason"]


async def test_intent_judge_failure_is_unconfirmed() -> None:
    class _Failing(StubLlm):
        async def complete(self, *, messages: list[dict[str, Any]], tools: Any) -> LoopTurn:
            if not self._turns:
                raise RuntimeError("judge down")
            return await _ScriptedLlm.complete(self, messages=messages, tools=tools)

    llm = _Failing([_tool("grep", pattern="x"), _answer(_finding(_OLD_BEHAVIOUR))])
    blob = await _round(llm, _only_after_box())
    assert blob["findings"][0]["confirmed"] is False
    assert "의도 충돌 판정" in blob["findings"][0]["reason"]


async def test_unreadable_intent_answer_is_unconfirmed() -> None:
    garbled = LoopTurn(content="maybe", tool_calls=())
    result, _, _ = await _basis_round(_finding(_OLD_BEHAVIOUR), _only_after_box(), garbled)
    assert result["confirmed"] is False


async def test_intent_judge_tokens_are_added_to_the_run() -> None:
    judge = LoopTurn(
        content='{"conflict": false}',
        tool_calls=(),
        usage_prompt_tokens=70,
        usage_completion_tokens=5,
    )
    result, _, run = await _basis_round(_finding(_OLD_BEHAVIOUR), _only_after_box(), judge)
    assert result["confirmed"] is True
    assert run.usage_prompt_tokens == 70
    assert run.usage_completion_tokens == 5


def test_parse_findings_reads_the_three_basis_kinds() -> None:
    raw = json.dumps(
        {
            "findings": [
                _finding(expected_from=ef)
                for ef in (
                    {"kind": "request", "quote": "q", "req_id": "R1"},
                    {"kind": "existing_test", "test": "a.py::t"},
                    {"kind": "unchanged_contract", "location": "a.py:1"},
                    {"kind": "request"},  # no quote: no basis
                )
            ]
        }
    )
    kinds = [f.expected_from.kind if f.expected_from else None for f in parse_findings(raw) or []]
    assert kinds == ["request", "existing_test", "unchanged_contract", None]


@pytest.mark.parametrize("quote", ["e", "the", "do it", "an  apple", "a b c d e f g"])
async def test_short_quotes_are_unconfirmed(quote: str) -> None:
    """A quote that is in the request but under 20 chars / 3 words proves nothing."""
    request = "the apple is red, e is a letter, do it an apple a b c d e f g"
    finding = _finding(_OLD_BEHAVIOUR, expected_from={"kind": "request", "quote": quote})
    result, llm, _ = await _basis_round(finding, _only_after_box(), request=request)
    assert result["confirmed"] is False
    assert "기대 근거 불일치" in result["reason"]
    assert len(llm.calls) == 2  # no intent call


async def test_intent_judge_is_skipped_when_tokens_are_exhausted() -> None:
    """Less cap left than one judge call needs → no call, unconfirmed with the budget reason."""
    big = LoopTurn(content=json.dumps({"tool": "grep", "args": {"pattern": "x"}}), tool_calls=())
    big = LoopTurn(content=big.content, tool_calls=(), usage_prompt_tokens=100_000)
    finding = _finding(_OLD_BEHAVIOUR)
    final = LoopTurn(
        content=json.dumps({"findings": [finding]}),
        tool_calls=(),
        usage_prompt_tokens=19_500,
        usage_completion_tokens=100,
    )
    llm = StubLlm([big, final])
    blob = await _round(llm, _only_after_box(), run=_intent_run("do it"))
    result = blob["findings"][0]
    assert result["confirmed"] is False
    assert result["reason"] == "토큰 예산 소진으로 의도 충돌 판정을 하지 못했다"
    assert len(llm.calls) == 2  # the judge was never called


# -- #1461 follow-up: a test repro whose file was never written ---------------------------------


async def test_unwritten_test_repro_is_missing_not_runtime_error() -> None:
    box = _AdvBox(pytest=_res(1, "1 failed"), pytest_before=_res(0, "1 passed"))
    box.written_only = True
    finding = await _one(box, _TEST_REPRO)  # answered with a path, no write_test before
    assert finding["confirmed"] is False
    assert finding["reason"] == "재현 테스트 파일이 없다(작성되지 않음)"
    assert finding["repro_missing"] is True
    assert "RuntimeError" not in finding["reason"]


async def test_written_test_repro_is_judged_as_before() -> None:
    box = _AdvBox(pytest=_res(1, "1 failed"), pytest_before=_res(0, "1 passed"))
    box.written_only = True
    llm = StubLlm(
        [
            _tool("write_test", path=_TEST_REPRO["path"], content="def test_x(): assert 0"),
            _answer(_finding()),
        ]
    )
    blob = await _round(llm, box)
    assert blob["findings"][0]["confirmed"] is True
    assert "repro_missing" not in blob["findings"][0]


async def test_final_answer_content_is_written_and_judged() -> None:
    box = _AdvBox(pytest=_res(1, "1 failed"), pytest_before=_res(0, "1 passed"))
    box.written_only = True
    repro = {**_TEST_REPRO, "content": "def test_x(): assert 0"}
    finding = await _one(box, repro)
    assert finding["confirmed"] is True
    assert _TEST_REPRO["path"] in box.written
    assert "content" not in finding["repro"]


async def test_final_answer_content_outside_scratch_or_too_big_is_refused() -> None:
    box = _AdvBox(pytest=_res(1, "1 failed"))
    box.written_only = True
    outside = await _one(box, {"kind": "test", "path": "tests/test_evil.py", "content": "x"})
    assert outside["confirmed"] is False
    assert "임시 테스트 디렉터리" in outside["reason"]
    big = {**_TEST_REPRO, "content": "x" * (MAX_TEST_CHARS + 1)}
    too_big = await _one(box, big)
    assert too_big["confirmed"] is False
    assert "너무 크다" in too_big["reason"]
    assert box.written == []


async def test_write_test_refuses_a_too_big_file_like_the_final_answer() -> None:
    box = _AdvBox()
    llm = StubLlm(
        [
            _tool("write_test", path=_TEST_REPRO["path"], content="x" * (MAX_TEST_CHARS + 1)),
            _answer(),
        ]
    )
    await _round(llm, box)
    assert box.written == []


def test_final_requests_tell_how_to_repro() -> None:
    assert "write_test" in adversarial_verifier._FINAL_REQUEST
    assert '"content"' in adversarial_verifier._FINAL_REQUEST
    assert "command repro" in adversarial_verifier._FINAL_REQUEST
    assert adversarial_verifier._FINAL_REQUEST.startswith(
        "Stop investigating. Give the final JSON now."
    )


# -- command_did_not_run: only a command that really could not run ------------------------------


def _not_run(code: int, out: str, **expect: Any) -> bool:
    return command_did_not_run(expect, exit_code=code, output=out, timed_out=False)


def test_command_that_exited_zero_did_not_run_is_false() -> None:
    assert not _not_run(0, "ModuleNotFoundError: No module named 'x'\n")
    assert not _not_run(0, "bash: foo: command not found\n")


def test_grep_hit_lines_are_not_a_missing_command() -> None:
    hit = "backend/x.py:12:    except ModuleNotFoundError:\n"
    assert not _not_run(1, hit)
    assert not _not_run(1, "see 'command not found' in docs\n")


def test_command_not_run_still_true_for_real_shell_and_import_errors() -> None:
    tb = "Traceback (most recent call last):\n  ...\nModuleNotFoundError: No module named 'dep'\n"
    assert _not_run(1, tb)
    assert _not_run(127, "")
    assert _not_run(127, "sh: 1: curl: not found\n")
    assert _not_run(2, "bash: foo: command not found\n")
    assert _not_run(2, "foo: command not found\n")
    assert not _not_run(1, tb, contains="No module named")  # the repro expects the error


async def test_grep_repro_exit_zero_with_error_text_is_confirmed_not_not_run() -> None:
    probe = "grep -rn ModuleNotFoundError backend"
    out = _res(0, "backend/x.py:12:    except ModuleNotFoundError:\n")
    repro = _cmd_repro(probe, exit_code=0, contains="backend/x.py:12")
    box = _AdvBox(scripted_before={probe: _res(1, "")}, **{probe: out})
    finding = await _one(box, repro)
    assert not finding.get("not_run")
    assert finding["confirmed"] is True
