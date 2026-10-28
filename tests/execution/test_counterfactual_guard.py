"""A guard test is checked against the code WITHOUT the change — does it still pass?

A test cited as the evidence for a guard item ("a foreign workspace's id is 404") proves the
guard only if it fails when the change is taken away. A 404 test for a route that does not
exist yet passes before the change too: green, cited, judged ``done`` — and guarding nothing.

So for each guard item judged done on cited tests, the run's non-test changes are put back to
the baseline, the cited test is run alone, and the tree is restored. Report-only (founder
2026-10-28): a test that still passes is MARKED, never failed — a regression test for
behaviour that already existed legitimately passes without the change.

Every case below runs real git and a real shell in a temporary repository.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from backend.workflow.application.counterfactual_check import check_guards
from backend.workflow.domain.counterfactual import is_test_path, parse_single_test_command
from backend.workflow.domain.requirements import GateFacts, public_rows, render_ledger_table
from backend.workflow.infrastructure.sandbox.noop_manager import NoopSandboxSession

_TEST = "tests/test_rotate.py::test_foreign_404"


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _repo(tmp_path: Path) -> tuple[Path, str]:
    """A repo whose run added ``NEW`` to src.py and a test file; returns (root, baseline)."""
    root = tmp_path / "ws"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "src.py").write_text("OLD\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    baseline = _git(root, "rev-parse", "HEAD")
    (root / "src.py").write_text("OLD\nNEW\n")
    (root / "added.py").write_text("brand new\n")
    (root / "tests").mkdir()
    (root / "tests" / "test_rotate.py").write_text("def test_foreign_404(): ...\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "work")
    return root, baseline


def _row(**over: Any) -> dict[str, Any]:
    return {
        "id": "R2",
        "text": "foreign workspace is 404",
        "kind": "guard",
        "status": "done",
        "paths": [],
        "tests": [_TEST],
        "note": "",
        "excerpt_hash": "h1",
        **over,
    }


_FACTS = GateFacts(
    diff_paths=frozenset({"src.py", "added.py", "tests/test_rotate.py"}),
    passed_commands=("pytest tests/",),
)


def _model(command: str) -> Any:
    calls: list[Any] = []

    async def complete(messages: list[dict[str, str]]) -> str:
        calls.append(messages)
        return json.dumps({"command": command})

    complete.calls = calls  # type: ignore[attr-defined]
    return complete


async def _check(root: Path, baseline: str, command: str, rows: list[dict[str, Any]]) -> Any:
    complete = _model(command)
    await check_guards(
        rows,
        box=NoopSandboxSession(str(root)),
        baseline=baseline,
        facts=_FACTS,
        complete=complete,
        venv_bin=None,
    )
    return complete


# ── the verdicts ─────────────────────────────────────────────────────────────


async def test_a_test_that_needs_the_change_holds(tmp_path: Path) -> None:
    root, baseline = _repo(tmp_path)
    rows = [_row()]
    await _check(root, baseline, "test -f tests/test_rotate.py && grep -q NEW src.py", rows)
    assert rows[0]["counterfactual"] == "holds"
    assert rows[0]["status"] == "done"


async def test_a_test_that_passes_without_the_change_is_marked_not_failed(tmp_path: Path) -> None:
    root, baseline = _repo(tmp_path)
    rows = [_row()]
    await _check(root, baseline, "test -f tests/test_rotate.py", rows)
    assert rows[0]["counterfactual"] == "vacuous"
    assert rows[0]["status"] == "done"  # report-only: the verdict is untouched


async def test_new_files_are_removed_for_the_check_too(tmp_path: Path) -> None:
    root, baseline = _repo(tmp_path)
    rows = [_row()]
    await _check(root, baseline, "test -f tests/test_rotate.py && test -f added.py", rows)
    assert rows[0]["counterfactual"] == "holds"


async def test_the_tree_is_exactly_restored(tmp_path: Path) -> None:
    root, baseline = _repo(tmp_path)
    head = _git(root, "rev-parse", "HEAD")
    await _check(root, baseline, "test -f tests/test_rotate.py", [_row()])
    assert _git(root, "status", "--porcelain") == ""
    assert _git(root, "rev-parse", "HEAD") == head
    assert (root / "src.py").read_text() == "OLD\nNEW\n"
    assert (root / "added.py").exists()


# ── when it must not run ─────────────────────────────────────────────────────


async def test_a_command_that_does_not_name_the_test_file_is_refused(tmp_path: Path) -> None:
    root, baseline = _repo(tmp_path)
    rows = [_row()]
    await _check(root, baseline, "true", rows)
    assert rows[0]["counterfactual"] == "unknown"


async def test_a_dirty_tree_is_never_touched(tmp_path: Path) -> None:
    root, baseline = _repo(tmp_path)
    (root / "src.py").write_text("uncommitted\n")
    rows = [_row()]
    await _check(root, baseline, "test -f tests/test_rotate.py", rows)
    assert rows[0]["counterfactual"] == "unknown"
    assert (root / "src.py").read_text() == "uncommitted\n"


async def test_only_guard_items_judged_done_on_tests_are_checked(tmp_path: Path) -> None:
    root, baseline = _repo(tmp_path)
    rows = [
        _row(id="R1", kind="do"),
        _row(id="R3", status="unclear"),
        _row(id="R4", tests=[]),
    ]
    complete = await _check(root, baseline, "test -f tests/test_rotate.py", rows)
    assert complete.calls == []
    assert all("counterfactual" not in r for r in rows)


async def test_a_verdict_is_reused_while_the_evidence_is_unchanged(tmp_path: Path) -> None:
    root, baseline = _repo(tmp_path)
    rows = [_row()]
    complete = _model("true")
    await check_guards(
        rows,
        box=NoopSandboxSession(str(root)),
        baseline=baseline,
        facts=_FACTS,
        complete=complete,
        venv_bin=None,
        prior_rows=[_row(counterfactual="holds", counterfactual_note="kept")],
    )
    assert rows[0]["counterfactual"] == "holds"
    assert complete.calls == []


# ── the pure parts ───────────────────────────────────────────────────────────


def test_test_paths_are_recognised_across_stacks() -> None:
    for p in (
        "tests/api/test_x.py",
        "apps/pwa/test/run.test.tsx",
        "src/__tests__/a.js",
        "pkg/thing_test.go",
        "web/a.spec.ts",
    ):
        assert is_test_path(p), p
    for p in ("backend/api/connectors.py", "apps/pwa/components/Run.tsx", "testing_utils/x.py"):
        assert not is_test_path(p), p


def test_the_single_test_command_must_name_the_test_file() -> None:
    assert parse_single_test_command('{"command": "pytest tests/test_rotate.py::x"}', _TEST)
    assert parse_single_test_command('{"command": "pytest -k x"}', _TEST) is None
    assert parse_single_test_command("not json", _TEST) is None


def test_a_vacuous_guard_shows_in_the_table_and_the_stored_rows() -> None:
    row = _row(counterfactual="vacuous", counterfactual_note="passes without the change")
    assert "passes without the change" in render_ledger_table([row])
    assert public_rows([row])[0]["counterfactual"] == "vacuous"


# ── wired into verification ──────────────────────────────────────────────────


async def test_verification_runs_the_check_only_for_a_round_that_passed(monkeypatch: Any) -> None:
    import backend.workflow.application.verification_service as vs
    from backend.workflow.domain.requirements import REQUIREMENTS_KEY
    from backend.workflow.domain.verifier_contract import VerificationCheck, VerificationContract
    from backend.workflow.infrastructure.sandbox import SandboxResult
    from tests._support import memory_session
    from tests.execution.test_verification_service import (
        FakeBox,
        StubLlm,
        _make_run,
        _make_step_and_attempt,
    )
    from tests.execution.test_verify_requirements_ledger import _FILES, _LEDGER, _STATUS, _judge

    seen: list[str] = []

    async def spy(rows: list[dict[str, Any]], **kwargs: Any) -> None:
        seen.append(kwargs["baseline"])

    monkeypatch.setattr(vs, "check_guards", spy)

    async def verify(command: str, llm: StubLlm) -> None:
        box = FakeBox(
            exec_map={
                "git -c core.quotePath=false status --porcelain --untracked-files=all": SandboxResult(
                    exit_code=0, stdout=_STATUS, stderr="", timed_out=False
                ),
                "false": SandboxResult(exit_code=1, stdout="", stderr="", timed_out=False),
            },
            files=dict(_FILES),
        )
        async with memory_session() as session:
            run = await _make_run(session)
            run.payload = {**run.payload, REQUIREMENTS_KEY: _LEDGER}
            step, attempt = await _make_step_and_attempt(session, run)
            await vs.VerificationService(session=session, llm=llm).verify(
                run=run,
                work_step=step,
                attempt=attempt,
                contract=VerificationContract(
                    checks=(VerificationCheck(kind="command", command=command),),
                    requirement_evidence=(
                        {"requirement_id": "R1", "paths": ["backend/api/connectors.py"]},
                        {"requirement_id": "R2", "tests": ["tests/api/test_rotate.py::test_x"]},
                    ),
                ),
                box=box,
                written_paths=[],
                final_text="done",
                baseline="base123",
            )

    ok = '{"passed": true, "reasoning": "ok"}'
    await verify("true", _judge(ok, ok))
    assert seen == ["base123"]
    await verify("false", _judge())
    assert seen == ["base123"]  # a failed round is not spent on it
