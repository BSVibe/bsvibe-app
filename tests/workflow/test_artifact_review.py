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
    METHOD_UNCERTAIN,
    NO_PRODUCTION_CALL,
    NewSymbol,
    _calls_section,
    masked_lines,
    new_public_symbols,
    parse_review,
    production_calls,
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


def test_a_symbol_only_in_comments_and_strings_has_no_production_call() -> None:
    sym = NewSymbol("close_widget", "backend/w.py")
    grep = (
        "backend/w.py:1:def close_widget(x):\n"
        "backend/a.py:3:    # close_widget(x) later\n"
        'backend/a.py:4:    msg = "call close_widget(x) now"\n'
        "backend/a.py:5:    note = 'close_widget'\n"
        "backend/a.py:6:    close_widget_all = 1\n"
        "backend/a.py:7:    obj.close_widget\n"
        "web/a.ts:2:  // close_widget(x)\n"
        "web/a.ts:3:  const s = `close_widget(x)`;\n"
        "web/a.ts:4:  const t = 'it\\'s close_widget(x)';\n"
    )

    assert production_calls(sym, grep) == []


def test_a_call_in_a_production_file_is_reported() -> None:
    sym = NewSymbol("close_widget", "backend/w.py")
    grep = (
        "backend/a.py:3:    # close_widget(x) later\n"
        "backend/b.py:9:    close_widget(x)\n"
        "backend/c.py:2:    close_widget (x)  # trailing\n"
        "backend/d.py:1:from backend.w import close_widget\n"
        "web/e.ts:5:  close_widget(x); // call\n"
        "tests/test_w.py:1:close_widget(x)\n"
    )

    assert production_calls(sym, grep) == [
        "backend/b.py:9:    close_widget(x)",
        "backend/c.py:2:    close_widget (x)  # trailing",
        "backend/d.py:1:from backend.w import close_widget",
        "web/e.ts:5:  close_widget(x); // call",
    ]


def test_a_method_counts_only_dot_calls_and_is_flagged_uncertain() -> None:
    diff = "+++ b/backend/w.py\n+class W:\n+    def run(self):\n"
    [_, method] = new_public_symbols(diff)
    assert method == NewSymbol("run", "backend/w.py", True)

    grep = "backend/a.py:1:    run(x)\nbackend/a.py:2:    job.run()\nbackend/a.py:3:    x = 'a.run()'\n"
    assert production_calls(method, grep) == ["backend/a.py:2:    job.run()"]
    section = _calls_section({method: ["backend/a.py:2:    job.run()"]})
    assert METHOD_UNCERTAIN in section


def test_a_bare_reference_and_multiline_import_count_as_uses() -> None:
    sym = NewSymbol("on_event", "backend/w.py")
    grep = (
        "backend/w.py:1:def on_event(e):\n"
        "backend/a.py:4:    register(handler=on_event)\n"
        "backend/b.py:2:    handlers = [on_event]\n"
        "backend/c.py:3:    on_event,\n"
        '    backend/d.py:1:__all__ = ["on_event"]\n'.lstrip()
        + "backend/e.py:5:    other.on_event\n"
        "backend/f.py:6:    # on_event is wired later\n"
    )

    assert production_calls(sym, grep) == [
        "backend/a.py:4:    register(handler=on_event)",
        "backend/b.py:2:    handlers = [on_event]",
        "backend/c.py:3:    on_event,",
    ]


def test_a_bound_method_reference_counts_for_a_method() -> None:
    method = NewSymbol("run", "backend/w.py", True)
    grep = "backend/a.py:2:    spawn(job.run)\nbackend/a.py:3:    run(x)\n"

    assert production_calls(method, grep) == ["backend/a.py:2:    spawn(job.run)"]


def test_unparseable_review_output_is_dropped() -> None:
    assert parse_review("not json") is None
    assert parse_review('{"findings": "x"}') is None
    assert parse_review('{"findings": [{"severity": "huge", "claim": "c"}]}') is None
    assert parse_review('{"findings": []}') == {"status": "done", "findings": []}


def _located(file: Any, line: Any) -> dict[str, Any]:
    finding = {**_FINDING, "file": file, "line": line}
    review = parse_review(json.dumps({"findings": [finding]}))
    assert review is not None
    return review["findings"][0]


def test_review_location_is_validated() -> None:
    for bad in ("a.py\n## x", "a`b`.py", "x" * 201, "a\x07.py", "   "):
        assert _located(bad, 3)["file"] is None
    assert _located("  src/a.py  ", 12)["file"] == "src/a.py"
    assert _located("x" * 200, 1)["file"] == "x" * 200
    for bad_line in (0, -3, True):
        assert _located("a.py", bad_line)["line"] is None
    assert _located("a.py", 12)["line"] == 12


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


_WIDGET_FILE = (
    "def run():\n"
    '    """Closes things.\n'
    "\n"
    "    close_widget(x) is what the caller does.\n"
    '    """\n'
    "    return 1\n"
)


def test_multiline_docstring_middle_line_is_not_a_production_call() -> None:
    sym = NewSymbol("close_widget", "backend/w.py")
    grep = "backend/a.py:4:    close_widget(x) is what the caller does.\n"

    # one line alone cannot tell it is inside a docstring ...
    assert production_calls(sym, grep) == [grep.strip()]
    # ... the whole file can
    masked = {"backend/a.py": masked_lines("backend/a.py", _WIDGET_FILE)}
    assert production_calls(sym, grep, masked) == []


def test_a_real_call_after_a_docstring_is_still_found() -> None:
    sym = NewSymbol("close_widget", "backend/w.py")
    text = _WIDGET_FILE + "close_widget(x)\n"
    masked = {"backend/a.py": masked_lines("backend/a.py", text)}
    grep = "backend/a.py:4:    close_widget(x) is what\nbackend/a.py:7:close_widget(x)\n"

    assert production_calls(sym, grep, masked) == ["backend/a.py:7:close_widget(x)"]


def test_ts_template_literal_lines_are_masked() -> None:
    text = "const s = `\n  close_widget(x)\n`;\n/* a\n close_widget(y) */\nclose_widget(z);\n"
    masked = masked_lines("a.ts", text)
    assert masked is not None
    assert [bool(line.strip()) for line in masked[:6]] == [True, False, True, False, False, True]
    assert "close_widget(z)" in masked[5]


_WRAPPED = """\
--- a/backend/w.py
+++ b/backend/w.py
@@ -1,2 +1,7 @@
 import os
+
+def fresh():
+    \"\"\"Takes a thing: a class
+    or constant of the module.
+    \"\"\"
+    return 1
"""


def test_wrapped_docstring_class_or_is_not_a_new_symbol() -> None:
    text = "import os\n\ndef fresh():\n" + _WRAPPED.split("def fresh():\n")[1].replace("+", "")
    source = text.replace("\\", "")
    assert new_public_symbols(_WRAPPED.replace("\\", "")) == [NewSymbol("fresh", "backend/w.py")]
    assert new_public_symbols(_WRAPPED.replace("\\", ""), {"backend/w.py": source}) == [
        NewSymbol("fresh", "backend/w.py")
    ]

    diff = (
        '+++ b/a.py\n+def g():\n+    """A class\n+    class or constant\n+    """\n+    return 1\n'
    )
    assert [s.name for s in new_public_symbols(diff)] == ["g"]


def test_nested_function_is_not_new_and_conditional_function_is_not_a_method() -> None:
    diff = (
        "+++ b/a.py\n"
        "+from typing import TYPE_CHECKING\n"
        "+if TYPE_CHECKING:\n"
        "+    def foo(): ...\n"
        "+def outer():\n"
        "+    def inner(): ...\n"
        "+class K:\n"
        "+    def m(self): ...\n"
        "+LIMIT = 3\n"
    )

    assert new_public_symbols(diff) == [
        NewSymbol("foo", "a.py", False),
        NewSymbol("outer", "a.py", False),
        NewSymbol("K", "a.py", False),
        NewSymbol("m", "a.py", True),
        NewSymbol("LIMIT", "a.py", False),
    ]


def test_type_checking_function_call_is_found() -> None:
    [foo] = new_public_symbols("+++ b/a.py\n+if True:\n+    def foo(): ...\n")
    assert not foo.is_method
    grep = "backend/b.py:3:    foo()\n"

    assert production_calls(foo, grep) == ["backend/b.py:3:    foo()"]
