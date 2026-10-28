"""The item judge is shown the part of a long diff the item is about — not its first 2,500 chars.

Prod #1204 re-run (d8ae99bd, 2026-10-27): R2 ("re-register the ingress — ``_register_telegram_
ingress``") came back ``unclear``: the judge read the head of a 330-line diff of
``backend/api/v1/connectors.py`` and the call it was asked about sat further down.
"""

from __future__ import annotations

from typing import Any

from backend.workflow.application.requirements_ledger import diff_excerpt
from backend.workflow.domain.requirements import relevant_hunks
from backend.workflow.infrastructure.sandbox import SandboxResult


def _hunk(n: int, body: str) -> str:
    return f"@@ -{n},3 +{n},3 @@\n+{body}\n" + "".join(f"+filler line {n}-{i}\n" for i in range(40))


_HEADER = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n"
_LONG = (
    _HEADER
    + "".join(_hunk(i * 100, f"def unrelated_{i}(): ...") for i in range(8))
    + _hunk(900, "    await _register_telegram_ingress(row, cipher=cipher)")
)


def test_a_diff_that_fits_is_shown_whole() -> None:
    short = _HEADER + _hunk(1, "x = 1")
    assert relevant_hunks(short, "anything", budget=10_000) == short


def test_the_hunk_naming_the_items_identifier_survives_the_cut() -> None:
    shown = relevant_hunks(
        _LONG, "re-register the ingress (`_register_telegram_ingress`)", budget=3000
    )
    assert len(shown) <= 3000 + 200
    assert "_register_telegram_ingress" in shown
    assert shown.startswith(_HEADER)
    assert "omitted" in shown


def test_with_nothing_to_match_the_head_is_kept() -> None:
    shown = relevant_hunks(_LONG, "속성이 그대로 유지된다", budget=3000)
    assert "unrelated_0" in shown


class _Box:
    workspace_mount = "/w"

    async def exec(self, command: str, **_k: Any) -> SandboxResult:
        return SandboxResult(exit_code=0, stdout=_LONG, stderr="", timed_out=False)

    async def read_file(self, rel_path: str, max_bytes: int) -> bytes:  # pragma: no cover
        return b""


async def test_the_judges_excerpt_is_cut_around_the_item() -> None:
    excerpt = diff_excerpt(_Box(), "abc123")  # type: ignore[arg-type]
    shown = await excerpt(
        {
            "id": "R2",
            "text": "텔레그램이면 인그레스를 다시 등록한다 (`_register_telegram_ingress`)",
            "paths": ["x.py"],
            "tests": [],
            "note": "",
        }
    )
    assert "_register_telegram_ingress" in shown


# ── one hunk larger than the whole budget ────────────────────────────────────
#
# Prod #1255 (run cf897406, 2026-10-28): three guard items cited tests in
# ``tests/mcp/test_workflow_tools.py``, whose diff was ONE 5,900-char hunk. Whole hunks only
# were kept, nothing fit, and the judge was shown no test body — all three came back unclear.

_ONE_BIG = _HEADER + (
    "@@ -630,6 +630,140 @@\n"
    # An early line that matches the item's words weakly (file and module names), far above
    # the test: only the cited test NAME can steer the window down to the body.
    + "+# helpers shared by tests/mcp/test_workflow_tools.py and test_runs_detail_other\n"
    + "".join(f"+    filler_{i} = {i}\n" for i in range(300))
    + "+async def test_runs_detail_lists_floor_and_final_checks(db) -> None:\n"
    + "+    assert by_kind['floor'].status == 'passed'\n"
    + "".join(f"+    tail_{i} = {i}\n" for i in range(300))
)


def test_an_oversized_hunk_is_cut_around_what_the_item_names() -> None:
    shown = relevant_hunks(
        _ONE_BIG,
        "`tests/mcp/test_workflow_tools.py::test_runs_detail_lists_floor_and_final_checks`",
        budget=1500,
    )
    assert len(shown) <= 1500 + 200
    assert "async def test_runs_detail_lists_floor_and_final_checks" in shown
    assert "by_kind['floor']" in shown


def test_an_oversized_hunk_with_no_match_keeps_its_head() -> None:
    shown = relevant_hunks(_ONE_BIG, "nothing here matches", budget=1500)
    assert "filler_0" in shown
    assert len(shown) <= 1500 + 200


async def test_the_cited_test_name_steers_the_excerpt() -> None:
    class _Big(_Box):
        async def exec(self, command: str, **_k: Any) -> SandboxResult:
            return SandboxResult(exit_code=0, stdout=_ONE_BIG, stderr="", timed_out=False)

    excerpt = diff_excerpt(_Big(), "abc123")  # type: ignore[arg-type]
    shown = await excerpt(
        {
            "id": "R5",
            "text": "통과한 검증의 런 상세 테스트",
            "paths": ["tests/mcp/test_workflow_tools.py"],
            "tests": [
                "tests/mcp/test_workflow_tools.py::test_runs_detail_lists_floor_and_final_checks"
            ],
            "note": "",
        }
    )
    assert "by_kind['floor']" in shown


def test_the_big_fixture_really_exceeds_the_judges_budget() -> None:
    from backend.workflow.application.requirements_ledger import _PATH_EXCERPT_CHARS

    assert len(_ONE_BIG) > _PATH_EXCERPT_CHARS * 2
