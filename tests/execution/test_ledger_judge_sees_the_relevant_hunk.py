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
