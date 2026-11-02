"""An item that cites several files shows the judge every one of them, not the first two.

Prod #1279 (run e8b49238, 2026-10-30): R1 cited the report, the REST queue, the MCP tool and
the domain module. The judge's note: "the ``_h_list_pending`` diff is cut midway, and the REST
endpoint's change does not appear at all" — ``unclear``. Each path was clipped to its own
5,000 chars and the joined text was then head-cut to what the cited tests left, so the first
paths spent the budget and the later ones vanished without a mark.

Now the room left after the tests is shared across the cited paths, each clipped around what
the item names, and a path that still does not fit is named as cut — never silently gone.
"""

from __future__ import annotations

from typing import Any

from backend.workflow.application.requirements_ledger import _EVIDENCE_CHARS, diff_excerpt
from backend.workflow.infrastructure.sandbox import SandboxResult

PATHS = [f"backend/mod{i}.py" for i in range(5)]


def _diff(i: int) -> str:
    header = f"diff --git a/{PATHS[i]} b/{PATHS[i]}\n--- a/{PATHS[i]}\n+++ b/{PATHS[i]}\n"
    filler = "".join(
        f"@@ -{n},3 +{n},3 @@\n" + "".join(f"+pad {n}-{k}\n" for k in range(40))
        for n in range(1, 13)
    )
    return header + filler + f"@@ -900,3 +900,3 @@\n+def footprint_{i}(): ...\n"


class _Box:
    workspace_mount = "/w"

    async def exec(self, command: str, **_k: Any) -> SandboxResult:
        i = next(n for n, p in enumerate(PATHS) if p in command)
        return SandboxResult(exit_code=0, stdout=_diff(i), stderr="", timed_out=False)

    async def read_file(self, rel_path: str, max_bytes: int) -> bytes:  # pragma: no cover
        return b""


async def test_every_cited_path_reaches_the_judge_around_what_the_item_names() -> None:
    assert all(len(_diff(i)) > 5000 for i in range(5))  # each alone would eat the budget
    excerpt = diff_excerpt(_Box(), "abc123")  # type: ignore[arg-type]
    shown = await excerpt(
        {
            "id": "R1",
            "text": "every surface reports it: " + ", ".join(f"`footprint_{i}`" for i in range(5)),
            "paths": PATHS,
            "tests": [],
            "note": "",
        }
    )

    for i in range(5):
        assert f"--- {PATHS[i]}" in shown
        assert f"def footprint_{i}()" in shown
    assert len(shown) <= _EVIDENCE_CHARS + 500


async def test_a_path_that_cannot_fit_is_named_as_cut() -> None:
    many = [f"backend/m{i}.py" for i in range(200)]

    class _ManyBox(_Box):
        async def exec(self, command: str, **_k: Any) -> SandboxResult:
            return SandboxResult(exit_code=0, stdout=_diff(0), stderr="", timed_out=False)

    excerpt = diff_excerpt(_ManyBox(), "abc123")  # type: ignore[arg-type]
    shown = await excerpt({"id": "R1", "text": "x", "paths": many, "tests": [], "note": ""})

    assert len(shown) <= _EVIDENCE_CHARS + 500
    assert "not shown" in shown
    assert "backend/m199.py" in shown  # named, even though its diff did not fit
