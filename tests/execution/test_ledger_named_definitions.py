"""The judge is shown the definitions an item names, even when they sit outside the diff's
relevant hunks (a config default, a constant, a function body)."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest import mock

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

    def __init__(
        self, files: dict[str, str], changed: list[str], untracked: list[str] | None = None
    ) -> None:
        self.files, self.changed, self.untracked = files, changed, untracked or []

    delay = 0.0

    async def exec(self, command: str, **_k: Any) -> SandboxResult:
        if self.delay:
            await asyncio.sleep(self.delay)
        if "ls-files" in command:
            out = "\n".join(self.untracked)
        elif "--name-only" in command:
            out = "\n".join(self.changed)
        else:
            out = _DIFF
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
    assert "no definition found in the files looked up: `no_such_name`" in shown
    assert "looked up: " in shown


async def test_a_name_with_no_definition_is_reported_to_the_judge() -> None:
    from backend.workflow.application.requirements_ledger import judge_messages

    box = _Box({"s.py": _SETTINGS}, ["s.py"])
    row = _row("R1", "`purge_expired` 를 추가한다")
    row["kind"] = "do"
    shown = await _excerpt(box)(row)
    assert "no definition found in the files looked up: `purge_expired`" in shown
    assert "does not exist" not in shown
    msgs = judge_messages(row, shown)
    assert "no definition found in the files looked up: `purge_expired`" in msgs[1]["content"]
    assert "files looked up" in msgs[0]["content"]


async def test_only_unfound_names_still_make_a_block() -> None:
    box = _Box({"s.py": _SETTINGS}, ["s.py"])
    shown = await _excerpt(box)(_row("R1", "`nope_a` and `nope_b`"))
    assert "`nope_a`, `nope_b`" in shown
    assert "s.py" in shown


async def test_found_and_skipped_names_are_not_listed_as_unfound() -> None:
    src = "class A:\n    limit: int = 2\n\n\nclass B:\n    limit: int = 2\n"
    box = _Box({"s.py": src}, ["s.py"])
    shown = await _excerpt(box)(_row("R1", "`A.limit` and `limit` and `ghost`"))
    line = [x for x in shown.splitlines() if "no definition found" in x][0]
    assert "`ghost`" in line
    assert "limit" not in line.split("looked up:")[1].split("—")[0]
    big = "".join(f"CONST_{i:03d} = '{'x' * 400}'\n" for i in range(30))
    box = _Box({"s.py": big}, ["s.py"])
    text = " ".join(f"`CONST_{i:03d}`" for i in range(30)) + " `ghost`"
    shown = await _excerpt(box)(_row("R1", text))
    line = [x for x in shown.splitlines() if "no definition found" in x][0]
    assert "CONST_" not in line.split("—")[0]


async def test_all_names_found_has_no_unfound_line() -> None:
    box = _Box({"s.py": _SETTINGS}, ["s.py"])
    shown = await _excerpt(box)(_row("R1", "`resolve_credentials`"))
    assert "no definition found" not in shown


async def test_many_unfound_names_stay_within_the_budget() -> None:
    from backend.workflow.application.requirements_ledger import _DEFINITIONS_CHARS

    big = "".join(f"CONST_{i:03d} = '{'x' * 400}'\n" for i in range(30))
    box = _Box({"s.py": big}, ["s.py"])
    ghosts = " ".join(f"`ghost_name_{i:03d}`" for i in range(80))
    text = " ".join(f"`CONST_{i:03d}`" for i in range(30)) + " " + ghosts
    shown = await _excerpt(box)(_row("R1", text))
    block = "--- named definitions" + shown.split("--- named definitions")[1]
    assert "no definition found" in block
    assert len(block) <= _DEFINITIONS_CHARS


def test_unfound_line_shrinks_to_fit_its_room() -> None:
    from backend.workflow.application.requirements_ledger import _unfound_line

    names = [f"ghost_name_{i:03d}" for i in range(80)]
    scope = [f"pkg/a_long_file_name_{i}.py" for i in range(9)]
    for room in (600, 250, 120, 60):
        line = _unfound_line(names, scope, room)
        assert "no definition found" in line
    assert "more name(s)" in _unfound_line(names, scope, 250)
    assert len(_unfound_line(names, scope, 600)) <= 600
    assert len(_unfound_line(names, scope, 1)) <= 120


async def test_names_past_the_cap_are_declared_not_shown() -> None:
    big = "".join(f"CONST_{i:03d} = '{'x' * 400}'\n" for i in range(30))
    box = _Box({"s.py": big}, ["s.py"])
    text = " ".join(f"`CONST_{i:03d}`" for i in range(30))
    shown = await _excerpt(box)(_row("R1", text))
    assert "not shown, out of room: CONST_0" in shown


def test_identifier_extraction() -> None:
    assert named_identifiers("`a/b.py` `Foo.bar_baz()` `t.py::test_it` `two words`") == [
        "Foo.bar_baz",
        "test_it",
    ]


_TWO_CLASSES = "class A:\n    limit: int = 1\n\n\nclass B:\n    limit: int = 2\n"


async def test_a_definition_cut_at_the_cap_says_so() -> None:
    body = "".join(f"    x{i} = {i}\n" for i in range(400))
    box = _Box({"s.py": f"def big():\n{body}"}, ["s.py"])
    shown = await _excerpt(box)(_row("R1", "`big` does it"))
    assert "[... truncated, " in shown
    assert " more chars ...]" in shown
    assert "x399" not in shown
    whole = await _excerpt(_Box({"s.py": _SETTINGS}, ["s.py"]))(_row("R1", "`resolve_credentials`"))
    assert "[... truncated" not in whole


async def test_a_qualified_name_gets_that_classes_definition() -> None:
    box = _Box({"s.py": _TWO_CLASSES}, ["s.py"])
    shown = await _excerpt(box)(_row("R1", "`B.limit` is 2"))
    assert "limit: int = 2" in shown
    assert "limit: int = 1" not in shown


async def test_the_same_definition_under_two_names_is_shown_once() -> None:
    src = "class A:\n    other: int = 1\n\n\nclass B:\n    limit: int = 2\n"
    box = _Box({"s.py": src}, ["s.py"])
    shown = await _excerpt(box)(_row("R1", "`B.limit` and `limit` are 2"))
    block = shown.split("--- named definitions")[1]
    assert block.count("limit: int = 2") == 1
    assert "# limit → same as above" in block


async def test_a_bare_name_with_two_locations_shows_both() -> None:
    box = _Box({"s.py": _TWO_CLASSES}, ["s.py"])
    shown = await _excerpt(box)(_row("R1", "`limit` is 2"))
    assert "s.py (A)" in shown
    assert "s.py (B)" in shown
    assert "limit: int = 1" in shown
    assert "limit: int = 2" in shown


async def test_a_bare_name_with_a_location_left_out_declares_it() -> None:
    big = "class A:\n    limit = '" + "x" * 1500 + "'\n"
    files = {f"m{i}.py": big.replace("class A", f"class C{i}") for i in range(6)}
    box = _Box(files, list(files))
    shown = await _excerpt(box)(_row("R1", "`limit` is set", paths=list(files)))
    assert "다른 정의" in shown
    assert "m5.py (C5)" in shown


async def test_a_qualifier_with_no_such_class_falls_back_to_the_bare_name() -> None:
    box = _Box({"s.py": _SETTINGS}, ["s.py"])
    shown = await _excerpt(box)(_row("R1", "`mod.schedule_max_per_workspace` is 20"))
    assert "schedule_max_per_workspace: int = 20" in shown


def test_typescript_definitions_are_found_without_ast() -> None:
    ts = "export const LIMIT = 5;\nclass A {\n  // cap\n  max: number = 3;\n}\n"
    assert named_definition(ts, "LIMIT", "a.ts") == "export const LIMIT = 5;"
    assert named_definition(ts, "max", "a.ts") == "  // cap\n  max: number = 3;"
    assert named_definition("def (", "x", "a.py") is None


async def test_each_file_is_parsed_once_however_many_names_are_looked_up() -> None:
    import ast

    files = {f"m{i}.py": "".join(f"N{i}_{j} = {j}\n" for j in range(10)) for i in range(3)}
    box = _Box(files, list(files))
    text = " ".join(f"`N0_{j}`" for j in range(10)) + " `nowhere_x`"
    real = ast.parse
    with mock.patch("ast.parse", side_effect=real) as parse:
        await _excerpt(box)(_row("R1", text, paths=list(files)))
    assert parse.call_count == 3


async def test_a_definition_in_an_untracked_new_file_is_shown() -> None:
    box = _Box({"new_mod.py": "LIMIT_NEW = 7\n"}, [], untracked=["new_mod.py"])
    shown = await _excerpt(box)(_row("R1", "`LIMIT_NEW` is 7"))
    assert "LIMIT_NEW = 7" in shown


async def test_json_and_markdown_files_are_not_searched() -> None:
    box = _Box({"a.json": "LIMIT_X = 1\n", "a.md": "LIMIT_X = 1\n"}, ["a.json", "a.md"])
    shown = await _excerpt(box)(_row("R1", "`LIMIT_X`", paths=["a.json"]))
    assert "named definitions" not in shown


async def test_concurrent_items_all_see_a_definition_in_an_uncited_changed_file() -> None:
    box = _Box({"s.py": _SETTINGS, "other.py": "y = 2\n"}, ["s.py", "other.py"])
    box.delay = 0.05
    excerpt = _excerpt(box)
    rows = [_row(f"R{i}", "`schedule_max_per_workspace` is 20") for i in range(3)]
    shown = await asyncio.gather(*(excerpt(r) for r in rows))
    assert all("schedule_max_per_workspace: int = 20" in s for s in shown)


_TS_TWO = """\
class Foo {
  run() {
    return 'foo-run';
  }
}

class Bar {
  run() {
    return 'bar-run';
  }
}
"""


async def test_ts_qualified_name_gets_that_classes_definition() -> None:
    box = _Box({"a.ts": _TS_TWO}, ["a.ts"])
    shown = await _excerpt(box)(_row("R1", "`Bar.run` works", paths=["a.ts"]))
    assert "bar-run" in shown
    assert "foo-run" not in shown


async def test_ts_bare_name_reveals_both_locations() -> None:
    box = _Box({"a.ts": _TS_TWO}, ["a.ts"])
    shown = await _excerpt(box)(_row("R1", "`run` works", paths=["a.ts"]))
    assert "foo-run" in shown
    assert "bar-run" in shown
    assert "a.ts (Foo)" in shown
    assert "a.ts (Bar)" in shown


async def test_out_of_room_names_still_give_locations() -> None:
    big = "".join(f"const CONST_{i:03d} = '{'x' * 400}';\n" for i in range(30))
    box = _Box({"s.ts": big + _TS_TWO.replace("'bar-run'", repr("y" * 7000))}, ["s.ts"])
    shown = await _excerpt(box)(
        _row("R1", " ".join(f"`CONST_{i:03d}`" for i in range(30)) + " `Bar.run`", paths=["s.ts"])
    )
    assert "out of room:" in shown
    room = shown.split("not shown, out of room:")[-1]
    assert "s.ts (Bar)" in room


async def test_ts_qualified_miss_is_never_shown_as_that_classes_definition() -> None:
    src = "class Bar {\n  go() {}\n}\n\nconst cfg = {\n  limit: 5,\n};\n"
    from backend.workflow.domain.requirements import ts_definition_candidates

    assert ts_definition_candidates(src, "Bar.limit") == []
    box = _Box({"b.ts": src}, ["b.ts"])
    shown = await _excerpt(box)(_row("R1", "`Bar.limit` is 5", paths=["b.ts"]))
    if "limit: 5" in shown:
        assert "b.ts" in shown


_TS_IFACE = """\
export interface Props {
  /** max rows */
  limit: number;
}

export type Opts = {
  depth: number;
};
"""


async def test_ts_interface_and_type_fields_are_found() -> None:
    box = _Box({"p.ts": _TS_IFACE}, ["p.ts"])
    for ident, expect in (
        ("Props.limit", "limit: number"),
        ("limit", "limit: number"),
        ("Opts.depth", "depth: number"),
    ):
        shown = await _excerpt(box)(_row("R1", f"`{ident}` ok", paths=["p.ts"]))
        assert expect in shown, ident
    from backend.workflow.domain.requirements import ts_definition_candidates

    assert [c for c, _ in ts_definition_candidates(_TS_IFACE, "limit")] == ["Props"]


async def test_ts_object_literal_field_found_as_last_resort() -> None:
    box = _Box({"o.ts": "const cfg = {\n  retries: 3,\n};\n"}, ["o.ts"])
    shown = await _excerpt(box)(_row("R1", "`retries` is 3", paths=["o.ts"]))
    assert "retries: 3" in shown


async def test_unfound_line_survives_long_skipped_and_notes() -> None:
    from backend.workflow.application.requirements_ledger import _DEFINITIONS_CHARS

    big = "".join(f"CONST_{i:03d} = '{'x' * 400}'\n" for i in range(30))
    files = {"s.py": big}
    changed = ["s.py"]
    for i in range(40):
        path = f"pkg/{'long_dir_' * 6}{i}/mod.py"
        files[path] = f"CONST_{i:03d} = 1\n"
        changed.append(path)
    box = _Box(files, changed)
    text = " ".join(f"`CONST_{i:03d}`" for i in range(30)) + " `ghost`"
    shown = await _excerpt(box)(_row("R1", text))
    block = "--- named definitions" + shown.split("--- named definitions")[1]
    assert "`ghost`" in block or "with no definition found" in block
    assert "no definition found" in block
    assert len(block) <= _DEFINITIONS_CHARS
