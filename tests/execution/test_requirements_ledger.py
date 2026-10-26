"""The requirements ledger, pure parts: what counts as an item, what counts as evidence.

v1 is deliberately narrow. A ledger exists only when the request STRUCTURES what it wants —
unticked checkboxes, or a list under a heading that names work to do, guards or a done
criterion. Free prose has no ledger (an agent cannot owe evidence for "make it nicer").
Evidence is a path in this run's diff, or a test whose file is in the diff or named by a
command that passed. Nothing is grounded by matching words in the agent's own summary.
"""

from __future__ import annotations

from backend.workflow.domain.requirements import (
    GateFacts,
    check_ledger,
    extract_requirements,
    ledger_closes,
    ledger_counts,
    ledger_passed,
    ledger_prompt,
    number_requirements,
    parse_requirement_evidence,
    render_ledger_failure,
)

ISSUE = """## 문제
설명. 이건 항목이 아니다.

- 문제 설명 속 목록도 항목이 아니다

## 할 것
- [ ] 백엔드 경로
- [x] 이미 끝난 일
- MCP 툴
  - 하위 설명은 항목이 아니다

## 재현 단계
1. 앱을 연다

## 가드 (먼저 빨간 걸 보라)
- 다른 워크스페이스는 404

## 범위 밖 (이 이슈에서 하지 마라)
- PWA 는 건드리지 마라
"""


def _texts(items: list[dict[str, str]]) -> list[tuple[str, str]]:
    return [(i["text"], i["kind"]) for i in items]


# ── extraction ───────────────────────────────────────────────────────────────


def test_items_come_from_checkboxes_and_from_lists_under_work_headings() -> None:
    assert _texts(extract_requirements(ISSUE)) == [
        ("백엔드 경로", "do"),
        ("MCP 툴", "do"),
        ("다른 워크스페이스는 404", "guard"),
        ("PWA 는 건드리지 마라", "constraint"),
    ]


def test_a_ticked_box_is_already_done_and_is_not_owed_again() -> None:
    assert "이미 끝난 일" not in [i["text"] for i in extract_requirements(ISSUE)]


def test_a_checkbox_counts_under_any_heading_and_with_none() -> None:
    text = "Please do this.\n\n- [ ] first\n* [ ] second\n\n## Notes\n- [ ] third"
    assert [i["text"] for i in extract_requirements(text)] == ["first", "second", "third"]


def test_prose_and_lists_under_other_headings_have_no_items() -> None:
    text = "## Steps to reproduce\n1. open\n2. click\n\nPlease fix login. Do not change the API."
    assert extract_requirements(text) == []


def test_english_headings_and_bold_headings_are_read() -> None:
    text = "**Acceptance criteria**\n- it works\n\n### Guards\n- a test pins it\n"
    assert _texts(extract_requirements(text)) == [("it works", "done"), ("a test pins it", "guard")]


def test_numbering_gives_stable_ids_and_drops_duplicates() -> None:
    items = number_requirements(
        [
            {"text": "A", "kind": "do", "source": "issue"},
            {"text": "a ", "kind": "do", "source": "comment"},
            {"text": "B", "kind": "guard", "source": "comment"},
        ]
    )
    assert [(i["id"], i["text"]) for i in items] == [("R1", "A"), ("R2", "B")]


# ── evidence ─────────────────────────────────────────────────────────────────

_LEDGER = number_requirements(
    [
        {"text": "백엔드 경로", "kind": "do", "source": "issue"},
        {"text": "404 가드", "kind": "guard", "source": "issue"},
        {"text": "PWA 는 건드리지 마라", "kind": "constraint", "source": "issue"},
    ]
)
_FACTS = GateFacts(
    diff_paths=frozenset({"backend/api/v1/connectors.py", "tests/api/test_rotate.py"}),
    passed_commands=("uv run pytest tests/api/test_rotate.py -q", "uv run pytest -q tests/mcp"),
)


def test_evidence_parses_tolerantly_and_the_last_entry_per_id_wins() -> None:
    parsed = parse_requirement_evidence(
        [
            {"requirement_id": "R1", "paths": ["a.py"]},
            "junk",
            {"requirement_id": "R1", "paths": ["./backend/api/v1/connectors.py:10-40"]},
            {"requirement_id": "R2", "status": "deferred"},
            {"paths": ["no id"]},
        ]
    )
    assert parsed == [
        {
            "requirement_id": "R1",
            "status": "done",
            "paths": ["backend/api/v1/connectors.py"],
            "tests": [],
            "note": "",
        },
        {"requirement_id": "R2", "status": "deferred", "paths": [], "tests": [], "note": ""},
    ]


def test_an_item_without_evidence_is_missing_and_the_ledger_fails() -> None:
    rows = check_ledger(_LEDGER, [], _FACTS)
    by_id = {r["id"]: r["status"] for r in rows}
    assert by_id == {"R1": "missing", "R2": "missing", "R3": "noted"}
    assert not ledger_passed(rows)
    assert "R1" in render_ledger_failure(rows) and "백엔드 경로" in render_ledger_failure(rows)


def test_a_path_in_the_diff_grounds_an_item_for_the_judge() -> None:
    evidence = parse_requirement_evidence(
        [{"requirement_id": "R1", "paths": ["backend/api/v1/connectors.py#L5-L9"]}]
    )
    [r1] = [r for r in check_ledger(_LEDGER, evidence, _FACTS) if r["id"] == "R1"]
    assert r1["status"] == "pending"
    assert r1["paths"] == ["backend/api/v1/connectors.py"]


def test_a_path_outside_the_diff_grounds_nothing() -> None:
    evidence = parse_requirement_evidence([{"requirement_id": "R1", "paths": ["README.md"]}])
    [r1] = [r for r in check_ledger(_LEDGER, evidence, _FACTS) if r["id"] == "R1"]
    assert r1["status"] == "missing"


def test_a_test_grounds_when_its_file_is_in_the_diff_or_a_passed_command_names_it() -> None:
    evidence = parse_requirement_evidence(
        [
            {"requirement_id": "R1", "tests": ["tests/api/test_rotate.py::test_x[param]"]},
            {"requirement_id": "R2", "tests": ["tests/mcp/test_tools.py::test_y"]},
        ]
    )
    rows = {r["id"]: r for r in check_ledger(_LEDGER, evidence, _FACTS)}
    assert rows["R1"]["status"] == "pending"  # file is in the diff
    assert rows["R2"]["status"] == "pending"  # `pytest -q tests/mcp` ran and passed it


def test_a_vague_test_reference_grounds_nothing() -> None:
    evidence = parse_requirement_evidence(
        [{"requirement_id": "R1", "tests": ["py"]}, {"requirement_id": "R2", "tests": ["passed"]}]
    )
    rows = {r["id"]: r["status"] for r in check_ledger(_LEDGER, evidence, _FACTS)}
    assert rows["R1"] == "missing" and rows["R2"] == "missing"


def test_a_deferred_item_needs_a_reason() -> None:
    evidence = parse_requirement_evidence(
        [
            {"requirement_id": "R1", "status": "deferred", "note": "needs a design decision"},
            {"requirement_id": "R2", "status": "deferred"},
        ]
    )
    rows = {r["id"]: r["status"] for r in check_ledger(_LEDGER, evidence, _FACTS)}
    assert rows["R1"] == "deferred"
    assert rows["R2"] == "missing"


# ── verdicts, closing, counts ───────────────────────────────────────────────


def _row(status: str, kind: str = "do") -> dict[str, object]:
    return {"id": "R1", "text": "t", "kind": kind, "status": status}


def test_only_done_items_pass_and_unclear_passes_but_is_counted() -> None:
    assert ledger_passed(
        [_row("done"), _row("unclear"), _row("deferred"), _row("noted", "constraint")]
    )
    assert not ledger_passed([_row("done"), _row("rejected")])
    assert not ledger_passed([_row("pending")])
    assert ledger_counts(
        [_row("done"), _row("deferred"), _row("unclear"), _row("noted", "constraint")]
    ) == {
        "requirements_total": 4,
        "requirements_done": 2,
        "requirements_deferred": 1,
    }


def test_the_pr_closes_the_issue_only_when_every_owed_item_is_done() -> None:
    assert ledger_closes([])
    assert ledger_closes([_row("done"), _row("noted", "constraint")])
    assert not ledger_closes([_row("done"), _row("deferred")])
    assert not ledger_closes([_row("unclear")])


def test_the_prompt_lists_every_item_and_says_how_to_cite_it() -> None:
    text = ledger_prompt(_LEDGER)
    for item in _LEDGER:
        assert item["id"] in text and item["text"] in text
    assert "requirement_evidence" in text


# ── second review (2026-10-26) ───────────────────────────────────────────────


def test_a_code_fence_is_neither_a_heading_nor_items() -> None:
    text = (
        "## 할 것\n```bash\n# 재현\nmake run\n```\n- 진짜 항목\n\n"
        "## Requirements\n```yaml\n- pydantic\n```\n"
    )
    assert [i["text"] for i in extract_requirements(text)] == ["진짜 항목"]


def test_only_a_test_runner_command_covers_a_test_directory() -> None:
    ledger = number_requirements([{"text": "A", "kind": "do"}])
    evidence = parse_requirement_evidence(
        [{"requirement_id": "R1", "tests": ["tests/test_old.py::test_a"]}]
    )
    lint = GateFacts(
        diff_paths=frozenset(), passed_commands=("uv run ruff check tests/", "ls tests")
    )
    runner = GateFacts(diff_paths=frozenset(), passed_commands=("uv run pytest -q tests/",))
    assert check_ledger(ledger, evidence, lint)[0]["status"] == "missing"
    assert check_ledger(ledger, evidence, runner)[0]["status"] == "pending"


def test_a_deferral_without_a_reason_says_so() -> None:
    ledger = number_requirements([{"text": "A", "kind": "do"}])
    rows = check_ledger(
        ledger, parse_requirement_evidence([{"requirement_id": "R1", "status": "deferred"}]), _FACTS
    )
    assert rows[0]["status"] == "missing"
    assert "reason" in render_ledger_failure(rows)


def test_a_file_in_a_new_untracked_directory_counts() -> None:
    # `git status --porcelain` names a new directory, not the files under it.
    facts = GateFacts(diff_paths=frozenset({"backend/new_pkg/"}), passed_commands=())
    ledger = number_requirements([{"text": "A", "kind": "do"}])
    evidence = parse_requirement_evidence(
        [{"requirement_id": "R1", "paths": ["backend/new_pkg/mod.py"]}]
    )
    assert check_ledger(ledger, evidence, facts)[0]["status"] == "pending"
