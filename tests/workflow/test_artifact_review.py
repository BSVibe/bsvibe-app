"""The independent review of a passed verification round — through ``VerificationService.verify``.

Only the LLM is a stub. The review is report-only: whatever it does, the round keeps its verdict.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from backend.workflow.application.agent_loop import LoopTurn
from backend.workflow.application.run_detail import _run_verification
from backend.workflow.application.verification_service import VerificationService
from backend.workflow.domain.artifact_review import (
    NO_PRODUCTION_CALL,
    NewSymbol,
    new_public_symbols,
    parse_review,
)
from backend.workflow.domain.requirements import REQUIREMENTS_KEY
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

_DIFF = """\
diff --git a/backend/svc.py b/backend/svc.py
--- /dev/null
+++ b/backend/svc.py
@@ -0,0 +1,12 @@
+def used_fn(x):
+    return x
+
+def dead_fn(x):
+    return x
+
+def only_test_fn(x):
+    return x
+
+class Widget:
+    def render(self):
+        return 1
+    def _private(self):
+        return 2
"""
_GREP = {
    "used_fn": (0, "backend/svc.py:1:def used_fn(x):\nbackend/api.py:7:    used_fn(payload)\n"),
    "dead_fn": (0, "backend/svc.py:4:def dead_fn(x):\n"),
    "only_test_fn": (
        0,
        "backend/svc.py:7:def only_test_fn(x):\ntests/test_svc.py:3:only_test_fn(1)\n",
    ),
    "Widget": (1, ""),
    "render": (1, ""),
}
_FINDING = {
    "severity": "high",
    "file": "backend/svc.py",
    "line": 4,
    "claim": "dead_fn is never called",
    "failure_scenario": "a webhook arrives -> nothing runs dead_fn -> the feature does nothing",
}


class _GitBox(FakeBox):
    """Answers the git diff and git grep the review asks; everything else like FakeBox."""

    def __init__(self, *diffs: str) -> None:
        super().__init__()
        self.diffs = list(diffs or (_DIFF,))

    async def exec(self, command: str, **kwargs: Any) -> SandboxResult:
        if " diff " in command and " HEAD" in command:
            diff = self.diffs.pop(0) if len(self.diffs) > 1 else self.diffs[0]
            return SandboxResult(exit_code=0, stdout=diff, stderr="", timed_out=False)
        if " grep " in command:
            name = command.split(" -e ")[1].split()[0].strip("'")
            code, out = _GREP.get(name, (1, ""))
            return SandboxResult(exit_code=code, stdout=out, stderr="", timed_out=False)
        return await super().exec(command, **kwargs)


def _review_turn(*findings: dict[str, Any]) -> LoopTurn:
    return LoopTurn(content=json.dumps({"findings": list(findings)}), tool_calls=[])


class _RaisingLlm(StubLlm):
    async def complete(self, *, messages: Any, tools: Any) -> LoopTurn:
        self.calls.append({"messages": list(messages), "tools": tools})
        raise TimeoutError("reviewer down")


async def _verify(
    llm: StubLlm,
    *,
    command: str = "true",
    box: FakeBox | None = None,
    rounds: int = 1,
    baseline: str | None = "base123",
) -> list[Any]:
    results = []
    async with memory_session() as session:
        run = await _make_run(session)
        run.payload = {
            **run.payload,
            "intent_text": "ORIGINAL-REQUEST 회전 경로를 추가한다",
            REQUIREMENTS_KEY: [
                {"id": "R1", "text": "LEDGER-ITEM", "kind": "constraint", "source": "issue"}
            ],
        }
        work_step, attempt = await _make_step_and_attempt(session, run)
        contract = VerificationContract(
            checks=(VerificationCheck(kind="command", command=command),)
        )
        svc = VerificationService(session=session, llm=llm)
        for _ in range(rounds):
            results.append(
                await svc.verify(
                    run=run,
                    work_step=work_step,
                    attempt=attempt,
                    contract=contract,
                    box=box or _GitBox(),
                    written_paths=[],
                    final_text="done",
                    baseline=baseline,
                )
            )
            await session.flush()
    return results


async def test_a_passed_round_stores_a_review() -> None:
    llm = StubLlm([_review_turn(_FINDING)])
    [vr] = await _verify(llm)

    assert vr.outcome is VerificationOutcome.PASSED
    assert vr.result["review"] == {"status": "done", "findings": [_FINDING]}
    [call] = llm.calls
    system, user = call["messages"][0], call["messages"][-1]
    assert system["role"] == "system" and "거부할 이유를 찾아라" in system["content"]
    assert len(call["messages"]) == 2  # a fresh conversation
    text = user["content"]
    assert "ORIGINAL-REQUEST" in text and "LEDGER-ITEM" in text
    assert "+def used_fn(x):" in text  # the diff
    assert "backend/api.py:7" in text  # where it is called
    assert "true" in text  # the gate command that ran


async def test_a_failed_round_gets_no_review() -> None:
    llm = StubLlm([])
    box = _GitBox()
    box._exec_map["false"] = SandboxResult(exit_code=1, stdout="", stderr="boom", timed_out=False)
    [vr] = await _verify(llm, command="false", box=box)

    assert vr.outcome is VerificationOutcome.FAILED
    assert "review" not in vr.result
    assert llm.calls == []


async def test_a_reviewer_that_raises_or_babbles_leaves_the_pass_unavailable() -> None:
    [raised] = await _verify(_RaisingLlm([]))
    [babbled] = await _verify(StubLlm([LoopTurn(content="looks fine to me", tool_calls=[])]))

    for vr in (raised, babbled):
        assert vr.outcome is VerificationOutcome.PASSED
        assert vr.result["review"]["status"] == "unavailable"
        assert vr.result["review"]["reason"]
        assert "findings" not in vr.result["review"]


async def test_a_new_function_nobody_calls_in_production_is_marked() -> None:
    llm = StubLlm([_review_turn()])
    [vr] = await _verify(llm)
    text = llm.calls[0]["messages"][-1]["content"]

    assert vr.result["review"] == {"status": "done", "findings": []}
    lines = {line.split(" (")[0].removeprefix("- "): line for line in text.splitlines()}
    assert NO_PRODUCTION_CALL not in lines["used_fn"]
    assert NO_PRODUCTION_CALL in lines["dead_fn"]
    assert NO_PRODUCTION_CALL in lines["only_test_fn"]  # called only from tests/
    assert NO_PRODUCTION_CALL in lines["Widget"] and NO_PRODUCTION_CALL in lines["render"]
    assert "_private" not in text.split("## 새 공개 심볼")[1]


async def test_same_tree_reverify_does_not_call_the_reviewer_again() -> None:
    llm = StubLlm([_review_turn(_FINDING)])  # a second call would exhaust the stub
    first, second = await _verify(llm, rounds=2)

    assert len(llm.calls) == 1
    assert second.result["review"] == first.result["review"]


async def test_a_changed_tree_is_reviewed_again() -> None:
    llm = StubLlm([_review_turn(), _review_turn()])
    first, second = await _verify(llm, box=_GitBox(_DIFF, _DIFF + "+# edited\n"), rounds=2)

    assert len(llm.calls) == 2
    assert first.result["review"]["status"] == second.result["review"]["status"] == "done"


def test_the_symbol_scan_skips_tests_privates_and_moved_definitions() -> None:
    diff = (
        "+++ b/a.py\n+def fresh():\n+def _hidden():\n-def moved():\n+def moved():\n"
        "+++ b/tests/test_a.py\n+def test_x():\n"
    )

    assert new_public_symbols(diff) == [NewSymbol("fresh", "a.py")]


def test_unparseable_review_output_is_dropped() -> None:
    assert parse_review("not json") is None
    assert parse_review('{"findings": "x"}') is None
    assert parse_review('{"findings": [{"severity": "huge", "claim": "c"}]}') is None
    assert parse_review('{"findings": []}') == {"status": "done", "findings": []}


def test_run_detail_exposes_the_review() -> None:
    review = {"status": "done", "findings": [_FINDING]}
    row = SimpleNamespace(
        id=uuid.uuid4(),
        outcome=VerificationOutcome.PASSED,
        created_at=datetime.now(UTC),
        result={"review": review},
    )

    assert _run_verification(row).review == review
    row.result = {}
    assert _run_verification(row).review is None
