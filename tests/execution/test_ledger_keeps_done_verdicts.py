"""A settled ``done`` item is not re-judged because the excerpt was cut differently."""

from __future__ import annotations

from typing import Any

from backend.workflow.application.requirements_ledger import (
    diff_excerpt,
    file_content_key,
    judge_ledger,
    judge_messages,
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
    prior: list[dict[str, Any]] = (),  # type: ignore[assignment]
    *,
    paths: list[str] | None = None,
    note: str = "",
) -> tuple[list[dict[str, Any]], list[Any]]:
    calls: list[Any] = []
    cited = paths or [_PATH]

    async def complete(messages: list[dict[str, str]]) -> str:
        calls.append(messages)
        return answer

    rows = await judge_ledger(
        [{k: _ROW[k] for k in ("id", "text", "kind")}],
        [
            {
                "requirement_id": "R1",
                "status": "done",
                "paths": cited,
                "tests": _ROW["tests"],
                "note": note,
            }
        ],
        GateFacts(diff_paths=frozenset(cited), passed_commands=("pytest",)),
        excerpt=diff_excerpt(box, "base"),  # type: ignore[arg-type]
        complete=complete,
        prior_rows=prior,
        content_key=file_content_key(box),  # type: ignore[arg-type]
    )
    return rows, calls


async def test_a_done_verdict_is_kept_when_the_excerpt_changes_but_the_files_do_not() -> None:
    box = _Box({_PATH: _PY})
    first, _ = await _round(box, '{"passed": true, "reasoning": "ok"}')
    assert first[0]["status"] == "done"
    # the cut moves (the paths come in another order) but no file changed
    flipped = _Box({_PATH: _PY, "src/a.py": "a\n", "src/b.py": "b\n"})
    box = _Box({_PATH: _PY, "src/b.py": "b\n", "src/a.py": "a\n"})
    first, _ = await _round(flipped, '{"passed": true}', paths=["src/a.py", "src/b.py"])
    second, calls = await _round(
        box, '{"passed": "cannot_determine"}', first, paths=["src/b.py", "src/a.py"]
    )
    assert second[0]["status"] == "done"
    assert calls == []


async def test_a_changed_file_is_judged_again() -> None:
    first, _ = await _round(_Box({_PATH: _PY}), '{"passed": true}')
    _, calls = await _round(_Box({_PATH: _PY + "\n# edit\n"}), '{"passed": true}', first)
    assert len(calls) == 1


async def test_same_file_helper_of_the_cited_test_is_in_the_excerpt() -> None:
    shown = await diff_excerpt(_Box({_PATH: _PY}), "base")(_ROW)  # type: ignore[arg-type]
    assert "def _seed_bound_repo" in shown


async def test_an_excerpt_not_shown_verdict_is_unclear_and_not_a_failure() -> None:
    rows, _ = await _round(
        _Box({_PATH: _PY}), '{"passed": "cannot_determine", "reasoning": "not in excerpt"}'
    )
    assert rows[0]["status"] == "unclear"
    assert ledger_passed(rows)


async def test_same_file_helper_outside_cited_paths_is_in_the_excerpt() -> None:
    row = {**_ROW, "paths": ["src/impl.py"]}
    box = _Box({_PATH: _PY, "src/impl.py": "def impl():\n    return 2\n"})
    shown = await diff_excerpt(box, "base")(row)  # type: ignore[arg-type]
    assert "def _seed_bound_repo" in shown
    # the helper sits under the test body, ahead of the cited paths
    assert shown.index("def _seed_bound_repo") < shown.index("--- src/impl.py")


async def test_helpers_survive_a_large_diff_right_under_the_test_body() -> None:
    big = "".join(f"line {n} about seeds a bound repo\n" for n in range(2000))
    row = {**_ROW, "paths": ["src/a.py", "src/b.py"]}
    box = _Box({_PATH: _PY, "src/a.py": big, "src/b.py": big})
    shown = await diff_excerpt(box, "base")(row)  # type: ignore[arg-type]
    assert len(shown) <= 10000 + 500
    assert "--- src/a.py" in shown
    assert shown.index("def test_alpha") < shown.index("def _seed_bound_repo")
    assert shown.index("def _seed_bound_repo") < shown.index("--- src/a.py")


_IMPORTING = """from tests.helpers import make_card


def test_alpha() -> None:
    assert make_card() == 1
"""


async def test_a_changed_imported_helper_is_judged_again() -> None:
    files = {_PATH: _IMPORTING, "tests/helpers.py": "def make_card():\n    return 1\n"}
    first, _ = await _round(_Box(files), '{"passed": true}')
    same, calls = await _round(_Box(dict(files)), '{"passed": true}', first)
    assert same[0]["status"] == "done" and calls == []
    changed = {**files, "tests/helpers.py": "def make_card():\n    return 2\n"}
    _, calls = await _round(_Box(changed), '{"passed": true}', first)
    assert len(calls) == 1


async def test_changed_evidence_is_judged_again() -> None:
    box = _Box({_PATH: _PY})
    first, _ = await _round(box, '{"passed": true}')
    _, calls = await _round(box, '{"passed": true}', first, note="now different")
    assert len(calls) == 1


def test_judge_prompt_reserves_rejected_for_conflict() -> None:
    system = judge_messages(_ROW, "x")[0]["content"]
    assert "CONFLICTS" in system
    assert "missing from the excerpt is not a conflict" in system
