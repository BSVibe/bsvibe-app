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
    artifact_planner_messages,
    drop_unasserted,
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


def test_judge_unavailable_when_probe_command_fails_to_parse() -> None:
    # L-measure 2026-07-02: the verifier wrote `python -c "…\ntry:\n…"` — literal
    # \n inside python -c is NOT a newline and dies with a SyntaxError. The probe
    # never ran the deliverable, so it must be unavailable, NOT contradicted (the
    # deliverable's factorial was in fact correct; a sibling probe matched).
    probe = Probe(name="neg", command='python -c "x\\ntry:\\n y"', expect_stdout_contains=("ok",))
    obs = Observation(
        exit_code=1,
        stderr="SyntaxError: unexpected character after line continuation character",
    )
    assert judge_probe(probe, obs) == "unavailable"


def test_judge_unavailable_on_shell_syntax_error() -> None:
    probe = Probe(name="p", command="run )(", expect_exit_zero=True)
    obs = Observation(exit_code=2, stderr="sh: syntax error near unexpected token `)'")
    assert judge_probe(probe, obs) == "unavailable"


def test_judge_unavailable_when_probe_cds_to_a_missing_absolute_path() -> None:
    # Dogfood 2026-07-06 (89397510): the demonstration PLANNER (a claude_code CLI
    # account) authored probes that `cd` into the executor's OWN host workdir
    # (``/private/var/folders/.../T/bsvibe-task-…``). That dir is gone by verify
    # time (verify runs in a FRESH /work clone), so every probe died with
    # ``sh: 1: cd: can't cd to <path>`` — judged CONTRADICTED → the whole
    # demonstration FALSE-FAILED a correct deliverable. A probe that could not
    # even enter its directory never exercised the deliverable → unavailable.
    probe = Probe(
        name="run chunk",
        command="cd /private/var/folders/xy/T/bsvibe-task-abc && uv run python -c 'from toolkit.lists import chunk; print(chunk([1,2,3],2))'",
        expect_stdout_contains=("[[1, 2], [3]]",),
    )
    obs = Observation(
        exit_code=2,
        stderr="sh: 1: cd: can't cd to /private/var/folders/xy/T/bsvibe-task-abc",
    )
    assert judge_probe(probe, obs) == "unavailable"


def test_judge_unavailable_on_bash_cd_no_such_directory() -> None:
    # Bash phrases the same failure differently ("cd: <path>: No such file or
    # directory") — both dash and bash wordings must downgrade, not false-fail.
    probe = Probe(name="p", command="cd /gone && ls", expect_exit_zero=True)
    obs = Observation(exit_code=1, stderr="bash: cd: /gone: No such file or directory")
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


# ── advisory fold — the ARTIFACT surface can only EARN evidence ───────────────
#
# A prose/data deliverable is demonstrated by probing the produced artifact, and
# the planner that writes those probes has NOT seen the artifact's text (§8.3 —
# it must not copy the answer out of what it is grading). That blindness is what
# keeps the evidence honest, and it is also why a contradiction there is WEAK:
# the planner is guessing at WORDING it never saw ("bloasis" vs "블로아시스"),
# so a miss means "not shown", never "the work is wrong". Code probes keep the
# strict fold — they call the deliverable and the machine answers.


def test_advisory_fold_never_fails_on_a_contradiction() -> None:
    assert summarize([_result("contradicted")], contradiction_fails=False) == "undemonstrable"
    assert (
        summarize([_result("matched"), _result("contradicted")], contradiction_fails=False)
        == "demonstrated"
    )


def test_advisory_fold_still_earns_demonstrated_on_a_match() -> None:
    assert summarize([_result("matched")], contradiction_fails=False) == "demonstrated"
    assert summarize([_result("unavailable")], contradiction_fails=False) == "undemonstrable"


def test_strict_fold_is_the_default_so_code_probes_are_unchanged() -> None:
    assert summarize([_result("contradicted")]) == "failed"


# ── the artifact planner is grounded in the TASK, never in the artifact ───────


def test_artifact_planner_names_the_files_but_carries_no_produced_text() -> None:
    messages = artifact_planner_messages(
        intent="write the weekly report covering both accounts",
        artifact_paths=["reports/weekly.md", "reports/data.csv"],
    )
    system = next(m["content"] for m in messages if m["role"] == "system")
    user = next(m["content"] for m in messages if m["role"] == "user")
    # It can NAME the artifact (a probe has to grep something) …
    assert "reports/weekly.md" in user
    assert "reports/data.csv" in user
    # … and it is told, in the open, that it has not been shown the contents and
    # must derive every expectation from the TASK.
    assert "have NOT been shown" in system
    assert "TASK" in system


# ── a probe that declares nothing cannot fail, so it is not evidence ──────────
#
# Live run e72689e8 (2026-08-14): the blind planner wrote six probes and TWO of
# them were `python -c "...; print('found' if ex else 'missing')"` with no
# declared substring. Python exits 0 either way, so they scored `matched` no
# matter what the artifact contained — the probe COMPUTED the answer and then
# threw it away. Six probes, two of them unfalsifiable, and the grade could not
# tell the difference.


def test_a_probe_with_no_declared_observation_is_dropped() -> None:
    asserted = Probe(name="a", command="grep -q x f.md", expect_stdout_contains=("x",))
    unasserted = Probe(name="b", command="python -c \"print('found')\"")
    plan = DemonstrationPlan(probes=(asserted, unasserted), setup=("prep",))
    kept = drop_unasserted(plan)
    assert kept.probes == (asserted,)
    assert kept.setup == ("prep",), "setup is preparation, not evidence — untouched"


def test_dropping_every_probe_leaves_an_empty_plan_not_a_pass() -> None:
    plan = DemonstrationPlan(probes=(Probe(name="b", command="true"),))
    assert drop_unasserted(plan).is_empty


def test_a_probe_expecting_failure_is_kept_even_without_a_substring() -> None:
    # `expect_exit_zero=False` IS a declared observation: the command must fail.
    # That probe can be contradicted (by succeeding), so it is real evidence.
    probe = Probe(name="rejects bad input", command="tool --bad", expect_exit_zero=False)
    assert drop_unasserted(DemonstrationPlan(probes=(probe,))).probes == (probe,)


def test_the_artifact_prompt_demands_a_declared_observation() -> None:
    system = next(
        m["content"]
        for m in artifact_planner_messages(intent="i", artifact_paths=["a.md"])
        if m["role"] == "system"
    )
    # It must say the expectation is REQUIRED, and say WHY — the trap is that a
    # command which prints its answer still exits 0.
    assert "REQUIRED" in system
    assert "exits 0" in system


# ── source_truncated / not_seen (planner blind-spot downgrade) ────────────────
#
# Root failure mode: the code planner reads only the first 8 KB of each source
# file. A function added at line 500 of a 1 000-line file is INVISIBLE to the
# planner, which may plan probes with wrong expectations. Those probes contradict
# the actual deliverable — but the PLANNER is at fault, not the deliverable.
#
# Fix: when the planner was given truncated source (Probe.source_truncated=True),
# judge_probe returns "not_seen" instead of "contradicted". summarize() treats
# "not_seen" the same as "unavailable" (downgrade to undemonstrable, not fail).
# The verification service sets source_truncated=True on every probe when
# any_source_truncated is detected in _run_outcome_demonstration.


def _obs(exit_code: int, stdout: str = "") -> Observation:
    return Observation(exit_code=exit_code, stdout=stdout, timed_out=False)


def test_judge_probe_returns_not_seen_for_truncated_source_contradiction() -> None:
    probe = Probe(
        name="check result",
        command='python -c "from m import f; print(f())"',
        expect_stdout_contains=("42",),
        source_truncated=True,
    )
    obs = _obs(exit_code=0, stdout="99")  # contradicts: "42" not in "99"
    assert judge_probe(probe, obs) == "not_seen"


def test_judge_probe_returns_matched_for_truncated_source_when_probe_passes() -> None:
    probe = Probe(
        name="check result",
        command='python -c "from m import f; print(f())"',
        expect_stdout_contains=("42",),
        source_truncated=True,
    )
    obs = _obs(exit_code=0, stdout="result: 42")
    assert judge_probe(probe, obs) == "matched"


def test_judge_probe_returns_contradicted_when_source_not_truncated() -> None:
    probe = Probe(
        name="check result",
        command='python -c "from m import f; print(f())"',
        expect_stdout_contains=("42",),
        source_truncated=False,
    )
    obs = _obs(exit_code=0, stdout="99")
    assert judge_probe(probe, obs) == "contradicted"


def test_not_seen_does_not_fail_summarize() -> None:
    not_seen_result = ProbeResult(
        probe=Probe(name="x", command="c", source_truncated=True),
        observation=_obs(exit_code=0, stdout="wrong"),
        status="not_seen",
    )
    assert summarize([not_seen_result]) == "undemonstrable"


def test_not_seen_alongside_matched_gives_demonstrated() -> None:
    matched = ProbeResult(
        probe=Probe(name="a", command="c", expect_stdout_contains=("ok",)),
        observation=_obs(exit_code=0, stdout="ok"),
        status="matched",
    )
    not_seen = ProbeResult(
        probe=Probe(name="b", command="d", source_truncated=True),
        observation=_obs(exit_code=0, stdout="wrong"),
        status="not_seen",
    )
    assert summarize([matched, not_seen]) == "demonstrated"


def test_probe_to_dict_includes_source_truncated_when_true() -> None:
    probe = Probe(name="x", command="c", source_truncated=True)
    assert probe.to_dict().get("source_truncated") is True


def test_probe_to_dict_omits_source_truncated_when_false() -> None:
    probe = Probe(name="x", command="c", source_truncated=False)
    assert "source_truncated" not in probe.to_dict()
