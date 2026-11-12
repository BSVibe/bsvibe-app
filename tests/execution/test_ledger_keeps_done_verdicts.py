"""A settled ``done`` item is not re-judged because the excerpt was cut differently."""

from __future__ import annotations

from typing import Any

from backend.workflow.application.requirements_ledger import (
    diff_excerpt,
    file_content_key,
    judge_ledger,
)
from backend.workflow.domain.requirements import GateFacts, ledger_passed
from backend.workflow.infrastructure.sandbox import SandboxResult

_PY = """def _seed_bound_repo():
    return 1


def test_alpha() -> None:
    assert _seed_bound_repo() == 1
"""
_PATH = "tests/test_x.py"
_ROW = {
    "id": "R1",
    "text": "seeds a bound repo",
    "kind": "do",
    "paths": [_PATH],
    "tests": [f"{_PATH}::test_alpha"],
    "note": "",
}


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


async def _round(
    box: _Box,
    answer: str,
    shown: str,
    prior: list[dict[str, Any]] = (),  # type: ignore[assignment]
) -> tuple[list[dict[str, Any]], list[Any]]:
    calls: list[Any] = []

    async def complete(messages: list[dict[str, str]]) -> str:
        calls.append(messages)
        return answer

    async def excerpt(_row: Any) -> str:
        return shown

    rows = await judge_ledger(
        [{k: _ROW[k] for k in ("id", "text", "kind")}],
        [{"requirement_id": "R1", "status": "done", "paths": [_PATH], "tests": _ROW["tests"]}],
        GateFacts(diff_paths=frozenset({_PATH}), passed_commands=("pytest",)),
        excerpt=excerpt,
        complete=complete,
        prior_rows=prior,
        content_key=file_content_key(box),  # type: ignore[arg-type]
    )
    return rows, calls


async def test_a_done_verdict_is_kept_when_the_excerpt_changes_but_the_files_do_not() -> None:
    box = _Box({_PATH: _PY})
    first, _ = await _round(box, '{"passed": true, "reasoning": "ok"}', "excerpt A: a then b")
    assert first[0]["status"] == "done"
    second, calls = await _round(
        box, '{"passed": "cannot_determine"}', "excerpt B: b then a", first
    )
    assert second[0]["status"] == "done"
    assert calls == []


async def test_a_changed_file_is_judged_again() -> None:
    first, _ = await _round(_Box({_PATH: _PY}), '{"passed": true}', "A")
    _, calls = await _round(_Box({_PATH: _PY + "\n# edit\n"}), '{"passed": true}', "B", first)
    assert len(calls) == 1


async def test_same_file_helper_of_the_cited_test_is_in_the_excerpt() -> None:
    shown = await diff_excerpt(_Box({_PATH: _PY}), "base")(_ROW)  # type: ignore[arg-type]
    assert "def _seed_bound_repo" in shown


async def test_an_excerpt_not_shown_verdict_is_unclear_and_not_a_failure() -> None:
    rows, _ = await _round(
        _Box({_PATH: _PY}), '{"passed": "cannot_determine", "reasoning": "not in excerpt"}', "x"
    )
    assert rows[0]["status"] == "unclear"
    assert ledger_passed(rows)
