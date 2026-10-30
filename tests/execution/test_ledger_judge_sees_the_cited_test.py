"""The item judge reads the cited test itself — and a cited test that does not exist fails the item.

Prod #1269 (run fd492229, 2026-10-30): four of five ledger items came back ``unclear``. Two of
them cited tests in a NEW file whose single 205-line hunk overflowed the judge's budget, so the
judge never saw the test body. And the names were wrong: ``test_cancel_mid_drive`` is the
FILE's name, and ``test_normal_run_still_delivers`` does not exist (the test is
``test_normal_run_still_creates_deliverable_and_card``). The ledger accepted both, because the
file was in the diff.

So a cited ``file::name`` is looked up in the file: its body goes to the judge, and a name that
is not defined there is a failed item with a reason — the agent can cite the right one.
"""

from __future__ import annotations

from typing import Any

from backend.workflow.application.requirements_ledger import (
    cited_tests,
    diff_excerpt,
    judge_ledger,
)
from backend.workflow.domain.requirements import GateFacts, cited_test_body
from backend.workflow.infrastructure.sandbox import SandboxResult

_PY = '''"""module doc mentions test_alpha in passing"""

import pytest


def helper():
    return 1


async def test_alpha(sf) -> None:
    result = await run()
    assert result.outcome == "cancelled"


def test_beta() -> None:
    assert helper() == 1
'''

_JS = """describe("runs", () => {
  it("shows the failed checks first", async () => {
    render(<RunDetail />);
    expect(groups).toEqual(["failed", "product"]);
  });

  it("other", () => {});
});
"""


def test_a_python_test_body_is_cut_from_its_definition_to_the_next() -> None:
    body = cited_test_body(_PY, "test_alpha")
    assert body is not None
    assert body.startswith("async def test_alpha")
    assert 'assert result.outcome == "cancelled"' in body
    assert "test_beta" not in body
    assert "module doc" not in body  # a mention is not the definition


def test_a_js_test_is_found_by_its_title() -> None:
    body = cited_test_body(_JS, "shows the failed checks first")
    assert body is not None
    assert 'expect(groups).toEqual(["failed", "product"])' in body
    assert "other" not in body


def test_a_name_that_is_not_defined_is_none() -> None:
    assert cited_test_body(_PY, "test_gamma") is None
    assert cited_test_body(_PY, "test_cancel_mid_drive") is None


class _Box:
    workspace_mount = "/w"

    def __init__(self, files: dict[str, str]) -> None:
        self._files = files

    async def exec(self, command: str, **_k: Any) -> SandboxResult:
        return SandboxResult(exit_code=0, stdout="", stderr="", timed_out=False)

    async def read_file(self, rel_path: str, max_bytes: int) -> bytes:
        if rel_path not in self._files:
            raise FileNotFoundError(rel_path)
        return self._files[rel_path].encode()[:max_bytes]


_ROW = {
    "id": "R4",
    "text": "a cancelled run writes nothing",
    "kind": "guard",
    "paths": [],
    "tests": ["tests/glue/test_cancel.py::test_alpha"],
    "note": "",
}


async def test_the_excerpt_carries_the_cited_test_body() -> None:
    box = _Box({"tests/glue/test_cancel.py": _PY})
    shown = await diff_excerpt(box, "base")(_ROW)  # type: ignore[arg-type]
    assert 'assert result.outcome == "cancelled"' in shown
    assert "def test_beta" not in shown


async def _judge(box: _Box, row: dict[str, Any]) -> tuple[list[dict[str, Any]], list[Any]]:
    calls: list[Any] = []

    async def complete(messages: list[dict[str, str]]) -> str:
        calls.append(messages)
        return '{"passed": true, "reasoning": "ok"}'

    requirements = [{k: row[k] for k in ("id", "text", "kind")}]
    evidence = [{"requirement_id": row["id"], "status": "done", "tests": row["tests"]}]
    facts = GateFacts(
        diff_paths=frozenset({"tests/glue/test_cancel.py"}), passed_commands=("pytest tests/",)
    )
    rows = await judge_ledger(
        requirements,
        evidence,
        facts,
        excerpt=diff_excerpt(box, "base"),  # type: ignore[arg-type]
        complete=complete,
        missing_tests=cited_tests(box),  # type: ignore[arg-type]
    )
    return rows, calls


async def test_a_cited_test_that_does_not_exist_fails_the_item_without_a_judge() -> None:
    box = _Box({"tests/glue/test_cancel.py": _PY})
    row = {**_ROW, "tests": ["tests/glue/test_cancel.py::test_cancel_mid_drive"]}
    rows, calls = await _judge(box, row)

    assert rows[0]["status"] == "rejected"
    assert "test_cancel_mid_drive" in rows[0]["note"]
    assert "not found" in rows[0]["note"]
    assert calls == []


async def test_an_existing_cited_test_is_judged() -> None:
    rows, calls = await _judge(_Box({"tests/glue/test_cancel.py": _PY}), dict(_ROW))
    assert rows[0]["status"] == "done"
    assert len(calls) == 1


async def test_an_unreadable_test_file_is_not_a_failure() -> None:
    rows, calls = await _judge(_Box({}), dict(_ROW))
    assert rows[0]["status"] == "done"  # judged as before; no lookup means no verdict from it
    assert len(calls) == 1


async def test_verification_rejects_a_cited_test_that_is_not_there() -> None:
    """Through ``VerificationService.verify``: the lookup is wired where the ledger is judged."""
    from backend.workflow.application.verification_service import VerificationService
    from backend.workflow.domain.requirements import LEDGER_RESULT_KEY, REQUIREMENTS_KEY
    from backend.workflow.domain.verifier_contract import VerificationCheck, VerificationContract
    from tests._support import memory_session
    from tests.execution.test_verification_service import (
        FakeBox,
        _make_run,
        _make_step_and_attempt,
    )
    from tests.execution.test_verify_requirements_ledger import _FILES, _LEDGER, _STATUS, _judge

    box = FakeBox(
        exec_map={
            "git -c core.quotePath=false status --porcelain --untracked-files=all": SandboxResult(
                exit_code=0, stdout=_STATUS, stderr="", timed_out=False
            )
        },
        files=dict(_FILES),
    )
    async with memory_session() as session:
        run = await _make_run(session)
        run.payload = {**run.payload, REQUIREMENTS_KEY: _LEDGER}
        step, attempt = await _make_step_and_attempt(session, run)
        vr = await VerificationService(
            session=session, llm=_judge('{"passed": true, "reasoning": "ok"}')
        ).verify(
            run=run,
            work_step=step,
            attempt=attempt,
            contract=VerificationContract(
                checks=(VerificationCheck(kind="command", command="true"),),
                requirement_evidence=(
                    {"requirement_id": "R1", "paths": ["backend/api/connectors.py"]},
                    {"requirement_id": "R2", "tests": ["tests/api/test_rotate.py::test_nope"]},
                ),
            ),
            box=box,
            written_paths=[],
            final_text="done",
        )
    rows = {r["id"]: r for r in vr.result[LEDGER_RESULT_KEY]}
    assert rows["R2"]["status"] == "rejected"
    assert "test_nope" in rows["R2"]["note"]
