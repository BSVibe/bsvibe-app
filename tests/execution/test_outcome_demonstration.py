"""Unit tests for the pure I2 outcome-demonstration schema + verdict.

The verdict must be a DETERMINISTIC function of the observation (no LLM in the
loop) — that is the property that keeps the half-judge from collapsing into a
pure judge (redesign SoT §2). These tests pin exactly that: parsing an
LLM-authored plan, judging one probe against one observation, and folding
probe results into a demonstration verdict.
"""

from __future__ import annotations

from backend.workflow.domain.outcome_demonstration import (
    MAX_PROBES,
    DemonstrationPlan,
    Observation,
    Probe,
    ProbeResult,
    judge_probe,
    parse_demonstration_plan,
    summarize,
)

# ── parsing ──────────────────────────────────────────────────────────────────


def test_parse_full_plan() -> None:
    plan = parse_demonstration_plan(
        {
            "setup": ["uv sync --frozen"],
            "probes": [
                {
                    "name": "factorial(5) == 120",
                    "command": "python -c 'from m import f; print(f(5))'",
                    "expect_stdout_contains": ["120"],
                    "expect_exit_zero": True,
                }
            ],
        }
    )
    assert plan.setup == ("uv sync --frozen",)
    assert len(plan.probes) == 1
    p = plan.probes[0]
    assert p.command.startswith("python -c")
    assert p.expect_stdout_contains == ("120",)
    assert p.expect_exit_zero is True


def test_parse_empty_or_garbage_yields_empty_plan() -> None:
    assert parse_demonstration_plan(None).is_empty
    assert parse_demonstration_plan({}).is_empty
    assert parse_demonstration_plan({"probes": []}).is_empty
    # a probe with no command is not a demonstration → dropped
    assert parse_demonstration_plan({"probes": [{"name": "x"}]}).is_empty


def test_parse_defaults_exit_zero_true() -> None:
    plan = parse_demonstration_plan({"probes": [{"command": "true"}]})
    assert plan.probes[0].expect_exit_zero is True
    assert plan.probes[0].expect_stdout_contains == ()


def test_parse_accepts_aliases() -> None:
    plan = parse_demonstration_plan(
        {"probes": [{"cmd": "run it", "stdout_contains": ["ok"], "exit_zero": False}]}
    )
    p = plan.probes[0]
    assert p.command == "run it"
    assert p.expect_stdout_contains == ("ok",)
    assert p.expect_exit_zero is False


def test_parse_caps_probe_count() -> None:
    plan = parse_demonstration_plan(
        {"probes": [{"command": f"c{i}"} for i in range(MAX_PROBES + 5)]}
    )
    assert len(plan.probes) == MAX_PROBES


# ── judge_probe (the deterministic verdict) ──────────────────────────────────


def test_judge_matched_on_exit_and_substring() -> None:
    probe = Probe(name="p", command="c", expect_stdout_contains=("120",))
    obs = Observation(exit_code=0, stdout="result: 120\n")
    assert judge_probe(probe, obs) == "matched"


def test_judge_contradicted_when_substring_absent() -> None:
    # The deliverable ran fine (exit 0) but did NOT produce the intended result.
    probe = Probe(name="p", command="c", expect_stdout_contains=("120",))
    obs = Observation(exit_code=0, stdout="result: 6\n")
    assert judge_probe(probe, obs) == "contradicted"


def test_judge_contradicted_when_exit_wrong() -> None:
    # Garbage-catcher: `grep 'new line' README` exits 1 because the intended
    # change was never made → contradicted → fails verification.
    probe = Probe(name="grep", command="grep x README", expect_exit_zero=True)
    obs = Observation(exit_code=1, stdout="", stderr="")
    assert judge_probe(probe, obs) == "contradicted"


def test_judge_expect_nonzero_exit() -> None:
    probe = Probe(name="reject", command="cli --bad", expect_exit_zero=False)
    assert judge_probe(probe, Observation(exit_code=2)) == "matched"
    assert judge_probe(probe, Observation(exit_code=0)) == "contradicted"


def test_judge_unavailable_on_timeout_or_none_exit() -> None:
    probe = Probe(name="p", command="c")
    assert judge_probe(probe, Observation(exit_code=None, timed_out=True)) == "unavailable"
    assert judge_probe(probe, Observation(exit_code=None)) == "unavailable"


def test_judge_unavailable_on_missing_command() -> None:
    probe = Probe(name="p", command="eslint .")
    obs = Observation(exit_code=127, stderr="eslint: command not found")
    assert judge_probe(probe, obs) == "unavailable"


def test_judge_unavailable_on_import_error_not_contradiction() -> None:
    # A wrong import path is the PROBE's fault, not the deliverable's — I1's
    # lint/type gate catches a genuinely broken source, so this downgrades
    # (unavailable) instead of false-failing good code.
    probe = Probe(name="p", command="python -c 'import wrong'", expect_stdout_contains=("120",))
    obs = Observation(exit_code=1, stderr="ModuleNotFoundError: No module named 'wrong'")
    assert judge_probe(probe, obs) == "unavailable"


# ── summarize (fold into one verdict) ────────────────────────────────────────


def _result(status: str) -> ProbeResult:
    p = Probe(name="p", command="c")
    return ProbeResult(probe=p, observation=Observation(exit_code=0), status=status)  # type: ignore[arg-type]


def test_summarize_any_contradiction_fails() -> None:
    assert summarize([_result("matched"), _result("contradicted")]) == "failed"


def test_summarize_matched_is_demonstrated() -> None:
    assert summarize([_result("matched"), _result("unavailable")]) == "demonstrated"


def test_summarize_no_matches_is_undemonstrable() -> None:
    assert summarize([]) == "undemonstrable"
    assert summarize([_result("unavailable")]) == "undemonstrable"


def test_empty_plan_is_empty() -> None:
    assert DemonstrationPlan().is_empty
    assert not DemonstrationPlan(probes=(Probe(name="p", command="c"),)).is_empty
