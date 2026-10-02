"""#1114 — a round that failed verification hands its OWN report to the next one.

An executor round is a whole fresh CLI session (``claude --print`` with no
``--resume`` — continuity is BSVibe's, never the executor's). Everything the
next session knows about the previous one is what the drive loop puts in
``messages``. On a failed verification the loop appended ONLY
``user: Verification FAILED …`` — the round's own final report
(``turn.content``) was dropped, and nothing said which files the run had
already changed. The next session read "fix the problem" with no idea what the
previous one had found or done, and re-explored from zero.

#1114 described this as "the whole conversation re-rendered every round". The
code shows the opposite gap: the seed context is re-rendered (unavoidable for a
fresh session), but the WORK is not handed over at all. These tests pin the
handover: the previous round's report and the run's changed files, as plain
text any executor can read.
"""

from __future__ import annotations

from pathlib import Path

from backend.workflow.application.agent_loop import LoopTurn, RunOrchestrator
from backend.workflow.infrastructure.sandbox import NoopSandboxManager
from tests._support import memory_session
from tests.execution.test_run_orchestrator import ScriptedLlm, _make_run, _tc

_FAILING_CHECK = {"kind": "command", "command": 'python3 -c "raise SystemExit(1)"'}


async def _run_one_failed_round(tmp_path: Path) -> ScriptedLlm:
    llm = ScriptedLlm(
        [
            LoopTurn(
                content="declaring and writing",
                tool_calls=(
                    _tc("declare_verification", checks=[_FAILING_CHECK]),
                    _tc("file_write", path="src/race.py", content="x = 1\n"),
                ),
            ),
            LoopTurn(content="ROUND_ONE_REPORT: the race is in transition()", tool_calls=()),
            LoopTurn(content="Done again.", tool_calls=()),
        ]
    )
    async with memory_session() as session:
        run = await _make_run(session)
        orch = RunOrchestrator(
            session=session,
            llm=llm,
            sandbox_manager=NoopSandboxManager(),
            max_cycles=3,
        )
        await orch.run(run=run, workspace_dir=tmp_path)
    return llm


async def test_the_next_round_sees_the_previous_rounds_report(tmp_path: Path) -> None:
    llm = await _run_one_failed_round(tmp_path)
    replan = llm.calls[-1]["messages"]
    assert any(
        m.get("role") == "assistant" and "ROUND_ONE_REPORT" in str(m.get("content")) for m in replan
    )


async def test_the_report_comes_before_the_failure(tmp_path: Path) -> None:
    """The conversation reads in order: what I did, then what the checks said."""
    llm = await _run_one_failed_round(tmp_path)
    replan = llm.calls[-1]["messages"]
    report_at = next(i for i, m in enumerate(replan) if "ROUND_ONE_REPORT" in str(m.get("content")))
    failure_at = next(
        i for i, m in enumerate(replan) if "Verification FAILED" in str(m.get("content"))
    )
    assert report_at < failure_at
    # The failure stays the LAST thing the agent reads (test_verification_feedback).
    assert replan[-1]["role"] == "user"


async def test_the_failure_names_the_files_the_run_has_changed(tmp_path: Path) -> None:
    llm = await _run_one_failed_round(tmp_path)
    failure = llm.calls[-1]["messages"][-1]["content"]
    assert "Files this run has changed so far:\n- src/race.py" in failure
