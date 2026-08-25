"""The failure feedback an agent receives must LEAD with what failed.

Run ``010bbdd8`` failed verification 16 times over 41 minutes on three
byte-identical mypy errors it never once saw. The feedback was
``json.dumps(verdict.result)[:1500]``, and the first 1500 characters of that
blob held nothing but the agent's own PASSING commands — the authoritative
``derived_gate`` failure sat at index 3516 and was cut off. The agent was
literally told "Verification FAILED. Details: [everything passed]".

These tests pin the property that makes that impossible: a failure report is
built from the failures, so no volume of passing output can crowd them out.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from backend.workflow.application.agent_loop import LoopTurn, RunOrchestrator
from backend.workflow.domain.verification_feedback import (
    render_failed_commands,
    render_verification_failure,
)
from backend.workflow.infrastructure.sandbox import NoopSandboxManager
from tests._support import memory_session
from tests.execution.test_run_orchestrator import ScriptedLlm, _make_run, _tc

_PYTEST_NOISE = "\n".join(f"tests/test_x.py::test_{i} PASSED  [{i}%]" for i in range(200))


def _result(**over: Any) -> dict[str, Any]:
    """A verdict.result shaped exactly like production's (key order included)."""
    base: dict[str, Any] = {
        "command_results": [
            {
                "command": "uv run pytest tests/execution/test_honesty.py -v",
                "exit_code": 0,
                "timed_out": False,
                "passed": True,
                "output": _PYTEST_NOISE,
            },
        ],
        "derived_gate": {
            "origin": "derived",
            "applicable": True,
            "commands": [
                {
                    "command": "ruff check backend/x.py",
                    "kind": "quality",
                    "status": "passed",
                    "exit_code": 0,
                    "timed_out": False,
                    "output": "All checks passed!\n",
                },
                {
                    "command": "mypy backend/x.py tests/test_x.py",
                    "kind": "quality",
                    "status": "failed",
                    "exit_code": 1,
                    "timed_out": False,
                    "output": (
                        "tests/test_x.py:13: error: Function is missing a type "
                        "annotation for one or more parameters  [no-untyped-def]\n"
                        "Found 1 error in 1 file (checked 2 source files)\n"
                    ),
                },
            ],
            "passed": False,
            "surface_exercised": False,
        },
        "judge": {"advisory": True, "skipped": "advisory_retrieval_only"},
        "outcome_demonstration": {"verdict": "demonstrated", "probes": [], "surface": "code"},
        "scope": {"verdict": "clean", "flagged_paths": [], "reasoning": "", "candidates": 2},
        "honesty_grade": None,
        "gate_expected": True,
        "work_gateable": True,
        "knowledge_refs": [],
    }
    base.update(over)
    return base


# ── the regression itself ────────────────────────────────────────────────────


def test_the_failing_check_survives_a_flood_of_passing_output() -> None:
    # command_results is serialised FIRST in production and carries a verbose
    # ``pytest -v`` log. A blind prefix spends the whole budget on it.
    text = render_verification_failure(_result())
    assert "no-untyped-def" in text
    assert "mypy backend/x.py tests/test_x.py" in text


def test_passing_detail_never_leads() -> None:
    # Whatever else the report carries, the first thing an agent reads about a
    # FAILED verification must be a failure.
    text = render_verification_failure(_result())
    head = text[:400]
    assert "mypy backend/x.py tests/test_x.py" in head
    assert "PASSED" not in head


def test_the_authoritative_gate_outranks_the_advisory_attestation() -> None:
    # verification_service treats derived_gate as authoritative and the agent's
    # own command_results as advisory. The report has to agree.
    res = _result()
    res["command_results"] = [
        {
            "command": "uv run pytest -q",
            "exit_code": 1,
            "timed_out": False,
            "passed": False,
            "output": "advisory failure text",
        },
    ]
    text = render_verification_failure(res)
    assert text.index("no-untyped-def") < text.index("advisory failure text")


# ── every failing surface reaches the agent ──────────────────────────────────


def test_a_contradicted_probe_reports_what_was_expected() -> None:
    res = _result(
        derived_gate=None,
        outcome_demonstration={
            "verdict": "failed",
            "surface": "code",
            "probes": [
                {
                    "name": "grade A returns Korean",
                    "command": "python -c 'print(explain(...))'",
                    "expect_exit_zero": True,
                    "expect_stdout_contains": ["게이트 통과"],
                    "exit_code": 0,
                    "timed_out": False,
                    "status": "contradicted",
                    "output": "gate passed\n",
                },
            ],
        },
    )
    text = render_verification_failure(res)
    assert "grade A returns Korean" in text
    assert "게이트 통과" in text
    assert "gate passed" in text


def test_a_judge_rejection_carries_its_reasoning() -> None:
    res = _result(
        derived_gate=None,
        judge={"passed": False, "reasoning": "the summary claims a migration that is absent"},
    )
    text = render_verification_failure(res)
    assert "the summary claims a migration that is absent" in text


def test_scope_flags_reach_the_agent() -> None:
    res = _result(
        derived_gate=None,
        scope={
            "verdict": "out_of_scope",
            "flagged_paths": ["backend/unrelated.py"],
            "reasoning": "touched a module the task never mentioned",
            "candidates": 3,
        },
    )
    text = render_verification_failure(res)
    assert "backend/unrelated.py" in text
    assert "touched a module the task never mentioned" in text


def test_a_timed_out_command_is_reported_as_a_failure() -> None:
    res = _result()
    res["derived_gate"]["commands"] = [
        {
            "command": "pytest tests/",
            "kind": "test",
            "status": "failed",
            "exit_code": -1,
            "timed_out": True,
            "output": "",
        },
    ]
    text = render_verification_failure(res)
    assert "pytest tests/" in text
    assert "timed out" in text.lower()


# ── honesty about what the report itself withholds ───────────────────────────


def test_korean_survives_verbatim_instead_of_escape_inflating() -> None:
    # json.dumps' default ensure_ascii turns one Hangul character into six
    # (진), so a Korean reasoning alone could eat the whole budget.
    res = _result(
        derived_gate=None,
        judge={"passed": False, "reasoning": "요약이 없는 마이그레이션을 주장합니다"},
    )
    text = render_verification_failure(res)
    assert "요약이 없는 마이그레이션을 주장합니다" in text
    assert "\\u" not in text


def test_truncation_announces_itself() -> None:
    res = _result()
    res["derived_gate"]["commands"][1]["output"] = "E" * 40_000
    text = render_verification_failure(res, budget=2_000)
    assert len(text) <= 2_000
    assert "omitted" in text


def test_a_long_output_keeps_both_ends() -> None:
    # mypy puts errors first, pytest puts the summary last. Keep both.
    res = _result()
    res["derived_gate"]["commands"][1]["output"] = (
        "HEAD-MARKER\n" + ("x" * 40_000) + "\nTAIL-MARKER"
    )
    text = render_verification_failure(res)
    assert "HEAD-MARKER" in text
    assert "TAIL-MARKER" in text


def test_the_budget_is_never_spent_on_passing_checks_before_a_failure() -> None:
    res = _result()
    res["command_results"][0]["output"] = "P" * 100_000
    text = render_verification_failure(res, budget=1_500)
    assert "no-untyped-def" in text
    assert len(text) <= 1_500


def test_a_failure_with_nothing_identifiable_says_exactly_that() -> None:
    # Reporting "everything passed" under a FAILED headline is the lie that
    # cost run 010bbdd8 forty-one minutes. Say the report found nothing instead.
    res = _result(derived_gate=None)
    text = render_verification_failure(res)
    assert "no specific check reported a failure" in text
    assert "PASSED" not in text


# ── the deriver's OWN failure (prod e6472fab, 2026-08-25) ────────────────────
#
# ``verification_service`` fails CLOSED when a toolchain manifest exists but the
# gate deriver could not run: ``gate_deriver_failed`` is persisted and the run
# FAILS. That is deliberate (INV-2). What was NOT deliberate is that the reason
# never reached the agent — this module read every other failing surface and
# none of them applied, so the agent was told "no specific check reported a
# failure" while the system knew exactly which one had.
#
# Measured cost on the first run it hit: six verification declarations, the
# agent widening its own gate each round guessing at what was missing, until it
# put a `grep`-for-forbidden-words gate over a test file full of UNRELATED
# pre-existing tests and blocked on a question. The lexical gate was the
# symptom; guessing in the dark was the defect.


def test_the_derivers_own_failure_reaches_the_agent() -> None:
    res = _result(
        derived_gate=None,
        gate_deriver_failed="deriver_unparseable",
    )
    text = render_verification_failure(res)
    assert "deriver_unparseable" in text
    # And it must not be reported as "nothing failed" — that is the lie.
    assert "no specific check reported a failure" not in text


def test_the_agent_is_told_not_to_change_its_work_over_a_harness_failure() -> None:
    """The agent's own checks all passed. Telling it only "FAILED" makes it
    edit code and widen gates at random — exactly what prod ``e6472fab`` did."""
    text = render_verification_failure(
        _result(derived_gate=None, gate_deriver_failed="deriver_unparseable")
    )
    lowered = text.lower()
    assert "harness" in lowered or "not your work" in lowered
    assert "do not" in lowered or "rather than" in lowered


def test_the_deriver_failure_outranks_the_agents_passing_attestation() -> None:
    """Authority order — the harness failure leads, as the derived gate would."""
    text = render_verification_failure(
        _result(derived_gate=None, gate_deriver_failed="deriver_unparseable")
    )
    assert text.index("deriver_unparseable") < len(text) // 2


def test_a_real_gate_failure_still_leads_over_the_deriver_note() -> None:
    """양성 대조군 — deriver 가 RAN 하면(=derived_gate 존재) 그 실패가 먼저다.
    이 케이스는 이 변경 전에도 후에도 같아야 한다."""
    text = render_verification_failure(_result())  # derived_gate present + failing
    assert "GATE COMMAND FAILED" in text
    assert text.index("GATE COMMAND FAILED") == 0


def test_no_deriver_note_when_the_deriver_did_not_fail() -> None:
    """음성 대조군 — 신호를 빼면 문구도 사라진다."""
    text = render_verification_failure(_result(derived_gate=None))
    assert "deriver" not in text.lower()


# ── the in-place gate caller (_loop_context) ─────────────────────────────────


def test_inplace_gate_commands_lead_with_the_failure_too() -> None:
    commands = [
        {
            "command": "ruff check .",
            "status": "passed",
            "exit_code": 0,
            "timed_out": False,
            "output": "All checks passed!\n" + ("z" * 50_000),
        },
        {
            "command": "pytest -q",
            "status": "failed",
            "exit_code": 1,
            "timed_out": False,
            "output": "1 failed, 880 passed",
        },
    ]
    text = render_failed_commands(commands)
    assert "1 failed, 880 passed" in text
    assert "z" * 100 not in text


def test_inplace_gate_with_no_failing_command_says_so() -> None:
    commands = [
        {
            "command": "ruff check .",
            "status": "passed",
            "exit_code": 0,
            "timed_out": False,
            "output": "ok",
        },
    ]
    text = render_failed_commands(commands)
    assert "no specific check reported a failure" in text


# ── the wiring: what the LLM actually receives on the next turn ──────────────
#
# The renderer being correct proves nothing about the loop using it. #748 and
# #750 both passed their unit tests and failed in production for exactly that
# reason — the piece was asserted, the assembled result was not. So this drives
# the real orchestrator loop, with no verifier double, and reads the message off
# the LLM stub. The failing check is deliberately placed behind ~3000 characters
# of passing output: under the old blind prefix it fell past the 1500-char cut.


async def test_the_agent_receives_the_failure_on_its_next_turn(tmp_path: Path) -> None:
    llm = ScriptedLlm(
        [
            LoopTurn(
                content="declaring the checks",
                tool_calls=(
                    _tc(
                        "declare_verification",
                        checks=[
                            {"kind": "command", "command": "python3 -c \"print('P' * 3000)\""},
                            {
                                "kind": "command",
                                "command": (
                                    "python3 -c \"print('MARKER_THE_AGENT_MUST_SEE');"
                                    ' raise SystemExit(1)"'
                                ),
                            },
                        ],
                    ),
                    _tc("file_write", path="answer.txt", content="42\n"),
                ),
            ),
            LoopTurn(content="Done.", tool_calls=()),
            LoopTurn(content="Done again.", tool_calls=()),
        ]
    )
    async with memory_session() as session:
        run = await _make_run(session)
        orch = RunOrchestrator(
            session=session,
            llm=llm,
            sandbox_manager=NoopSandboxManager(),
            # One LLM turn per cycle: declare+write, summarise (→ verify fails),
            # then the re-plan turn this test exists to read.
            max_cycles=3,
        )
        await orch.run(run=run, workspace_dir=tmp_path)

    # The turn that follows the failed verification.
    replan = llm.calls[-1]["messages"][-1]
    assert replan["role"] == "user"
    assert "Verification FAILED" in replan["content"]
    assert "MARKER_THE_AGENT_MUST_SEE" in replan["content"]
    # And the passing command's 3000 characters did not get to bury it.
    assert "P" * 500 not in replan["content"]
