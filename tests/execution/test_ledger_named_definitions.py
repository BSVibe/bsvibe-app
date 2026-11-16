"""The judge is shown the definitions an item names, even when they sit outside the diff's
relevant hunks (a config default, a constant, a function body)."""

from __future__ import annotations

from typing import Any

from backend.workflow.application.requirements_ledger import diff_excerpt
from backend.workflow.domain.requirements import named_definition, named_identifiers
from backend.workflow.infrastructure.sandbox import SandboxResult

_SETTINGS = """\
class Settings:
    other: int = 1
    # Most schedules one workspace may hold.
    schedule_max_per_workspace: int = 20
    more: int = 2


PLAN_LIMITS = {"free": 1}


def resolve_credentials(row):
    value = row.get("k")
    return value
"""

_HEADER = "diff --git a/s.py b/s.py\n--- a/s.py\n+++ b/s.py\n"
_DIFF = _HEADER + "@@ -1,3 +1,3 @@\n+x = 1\n"


class _Box:
    workspace_mount = "/w"

    def __init__(self, files: dict[str, str], changed: list[str]) -> None:
        self.files, self.changed = files, changed

    async def exec(self, command: str, **_k: Any) -> SandboxResult:
        out = "\n".join(self.changed) if "--name-only" in command else _DIFF
        return SandboxResult(exit_code=0, stdout=out, stderr="", timed_out=False)

    async def read_file(self, rel_path: str, max_bytes: int) -> bytes:
        if rel_path not in self.files:
            raise FileNotFoundError(rel_path)
        return self.files[rel_path].encode()


def _row(rid: str, text: str, paths: list[str] | None = None) -> dict[str, Any]:
    return {"id": rid, "text": text, "paths": paths or ["other.py"], "tests": [], "note": ""}


def _excerpt(box: _Box):
    return diff_excerpt(box, "abc123")  # type: ignore[arg-type]


async def test_a_default_outside_the_relevant_hunk_is_shown() -> None:
    box = _Box({"s.py": _SETTINGS, "other.py": "y = 2\n"}, ["s.py", "other.py"])
    shown = await _excerpt(box)(_row("R1", "`schedule_max_per_workspace` 의 기본값은 20"))
    assert "named definitions" in shown
    assert "schedule_max_per_workspace: int = 20" in shown
    assert "# Most schedules one workspace may hold." in shown


async def test_without_a_backticked_name_there_is_no_block() -> None:
    box = _Box({"s.py": _SETTINGS}, ["s.py"])
    shown = await _excerpt(box)(_row("R1", "기본값은 20"))
    assert "named definitions" not in shown


async def test_two_items_naming_the_same_thing_see_the_same_definition() -> None:
    box = _Box({"s.py": _SETTINGS}, ["s.py"])
    excerpt = _excerpt(box)
    a = await excerpt(_row("R1", "`schedule_max_per_workspace` is 20"))
    b = await excerpt(_row("R2", "limit read from `Settings.schedule_max_per_workspace`"))
    assert "schedule_max_per_workspace: int = 20" in a
    assert "schedule_max_per_workspace: int = 20" in b
    assert a.split("--- named definitions")[1] == b.split("--- named definitions")[1]


async def test_a_function_is_shown_whole_and_a_missing_name_is_skipped() -> None:
    box = _Box({"s.py": _SETTINGS}, ["s.py"])
    shown = await _excerpt(box)(_row("R1", "`resolve_credentials` and `no_such_name`"))
    assert "return value" in shown
    assert "no_such_name" not in shown


async def test_names_past_the_cap_are_declared_not_shown() -> None:
    big = "".join(f"CONST_{i:03d} = '{'x' * 400}'\n" for i in range(30))
    box = _Box({"s.py": big}, ["s.py"])
    text = " ".join(f"`CONST_{i:03d}`" for i in range(30))
    shown = await _excerpt(box)(_row("R1", text))
    assert "not shown, out of room: CONST_0" in shown


def test_identifier_extraction() -> None:
    assert named_identifiers("`a/b.py` `Foo.bar_baz()` `t.py::test_it` `two words`") == [
        "bar_baz",
        "test_it",
    ]


def test_typescript_definitions_are_found_without_ast() -> None:
    ts = "export const LIMIT = 5;\nclass A {\n  // cap\n  max: number = 3;\n}\n"
    assert named_definition(ts, "LIMIT", "a.ts") == "export const LIMIT = 5;"
    assert named_definition(ts, "max", "a.ts") == "  // cap\n  max: number = 3;"
    assert named_definition("def (", "x", "a.py") is None
