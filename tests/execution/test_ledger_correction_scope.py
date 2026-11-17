"""A founder correction re-judges only the ledger rows it is about (#1375 family)."""

from __future__ import annotations

from typing import Any

from backend.workflow.application.requirements_ledger import judge_ledger
from backend.workflow.domain.requirements import (
    GateFacts,
    append_founder_correction,
    founder_correction_items,
    founder_corrections,
    infer_correction_items,
)

_REQS = [
    {"id": f"R{n}", "text": f"item {n} touches `thing_{n}`", "kind": "do"} for n in range(1, 5)
]
_EVIDENCE = [
    {"requirement_id": r["id"], "status": "done", "paths": ["a.py"], "tests": [], "note": ""}
    for r in _REQS
]
_FACTS = GateFacts(diff_paths=frozenset({"a.py"}), passed_commands=())


async def _excerpt(_row: Any) -> str:
    return "--- a.py\nx = 1"


async def _content_key(row: Any) -> str:
    return f"content-{row['id']}"


async def _round(
    corrections: list[str],
    items: list[tuple[str, ...]],
    prior: list[dict[str, Any]] = (),  # type: ignore[assignment]
) -> tuple[list[dict[str, Any]], list[list[dict[str, str]]]]:
    calls: list[list[dict[str, str]]] = []

    async def complete(messages: list[dict[str, str]]) -> str:
        calls.append(messages)
        return '{"passed": true, "reasoning": "ok"}'

    rows = await judge_ledger(
        _REQS,
        _EVIDENCE,
        _FACTS,
        excerpt=_excerpt,
        content_key=_content_key,
        complete=complete,
        prior_rows=prior,
        corrections=corrections,
        correction_items=items,
    )
    return rows, calls


def _asked(calls: list[list[dict[str, str]]]) -> int:
    return len(calls)


async def test_correction_about_r2_rejudges_only_r2() -> None:
    first, calls = await _round([], [])
    assert _asked(calls) == 4
    assert all(r["status"] == "done" for r in first)

    text = "R2 는 값이 아니라 플래그다"
    second, calls = await _round([text], [("R2",)], first)
    assert _asked(calls) == 1
    assert "item 2" in calls[0][1]["content"]
    # the judge still gets the whole list, numbered
    assert f"- C1: {text}" in calls[0][1]["content"]
    assert all(r["status"] == "done" for r in second)


async def test_unscoped_correction_rejudges_everything() -> None:
    first, _ = await _round([], [])
    _, calls = await _round(["전반적으로 다시 봐라"], [()], first)
    assert _asked(calls) == 4


async def test_later_correction_keeps_earlier_scoped_rows_cached() -> None:
    first, _ = await _round([], [])
    one, _ = await _round(["R2 는 틀렸다"], [("R2",)], first)
    _, calls = await _round(["R2 는 틀렸다", "R3 도 틀렸다"], [("R2",), ("R3",)], one)
    assert _asked(calls) == 1
    assert "item 3" in calls[0][1]["content"]


def test_infers_items() -> None:
    assert infer_correction_items("R2 는 틀렸고 R4 도", _REQS) == ["R2", "R4"]
    assert infer_correction_items("R9 는 없다", _REQS) == []
    assert infer_correction_items("`thing_3` 을 바꿔라", _REQS) == ["R3"]
    assert infer_correction_items("R1 은 `thing_3` 이다", _REQS) == ["R1"]
    assert infer_correction_items("그냥 다시", _REQS) == []


def test_the_scope_is_stored_with_the_correction() -> None:
    payload: dict[str, Any] = {"requirements": _REQS}
    append_founder_correction(payload, "d1", "R2 는 틀렸다")
    append_founder_correction(payload, "d2", "전부 틀렸다")
    assert payload["founder_corrections"][0]["items"] == ["R2"]
    assert payload["founder_corrections"][1]["items"] == []
    assert founder_corrections(payload) == ["R2 는 틀렸다", "전부 틀렸다"]
    assert founder_correction_items(payload) == [("R2",), ()]
