"""Code a run cites but did not change reaches the judge around what the item names.

Prod #1046 re-run (run 815740af, 2026-11-02): the work was already on main, so R1 ("make
``telegram_callback._is_authorized_founder`` the allowlist rule") was evidenced by the file
as it stands — with no diff against the baseline. The judge was shown the file's first 5,000
chars, the function sat further down, and R1 came back ``unclear``.

Now a cited path with no diff is shown as the file itself, cut around the lines the item
names — the same way a long diff is.
"""

from __future__ import annotations

from typing import Any

from backend.workflow.application.requirements_ledger import diff_excerpt
from backend.workflow.infrastructure.sandbox import SandboxResult

_FILE = (
    '"""Telegram callbacks."""\n\n'
    + "".join(f"def helper_{i}(x):\n    return x + {i}\n\n" for i in range(600))
    + "def _is_authorized_founder(account, from_id):\n"
    + "    return is_authorized_tapper(account.delivery_config, from_id)\n"
)


class _Box:
    workspace_mount = "/w"
    asked: list[int] = []

    async def exec(self, command: str, **_k: Any) -> SandboxResult:
        return SandboxResult(exit_code=0, stdout="", stderr="", timed_out=False)  # no diff

    async def read_file(self, rel_path: str, max_bytes: int) -> bytes:
        self.asked.append(max_bytes)
        return _FILE.encode()[:max_bytes]


async def test_an_unchanged_cited_file_is_shown_around_the_named_function() -> None:
    assert len(_FILE) > 15_000 and _FILE.index("_is_authorized_founder") > 15_000
    excerpt = diff_excerpt(_Box(), "abc123")  # type: ignore[arg-type]
    shown = await excerpt(
        {
            "id": "R1",
            "text": "`telegram_callback._is_authorized_founder` → 슬랙/디스코드와 같은 허용목록 판정으로",
            "paths": ["backend/connectors/telegram_callback.py"],
            "tests": [],
            "note": "",
        }
    )

    assert "def _is_authorized_founder" in shown
    assert "is_authorized_tapper" in shown
    assert "no diff" in shown  # the judge is told it is the file as it stands, not a change
