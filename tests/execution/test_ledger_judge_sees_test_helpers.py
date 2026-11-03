"""The judge reading a cited test also reads the same-file helpers and fixtures it uses.

Prod #1287 (run 00065c14, 2026-11-02): R5 ("real rows through the real handlers — never a
hand-built response") and R6 ("only the unclear and deferred items") came back ``unclear``.
The judge was shown the test bodies, which call ``_hold`` / ``_mcp`` / ``_rest`` and read
``MIXED`` — all defined higher up in the same file and not shown, so it could not tell what
they stored or what the fixture held.

Now a cited test's body is followed by the module-level definitions it names from its own file.
"""

from __future__ import annotations

from typing import Any

from backend.workflow.application.requirements_ledger import diff_excerpt
from backend.workflow.domain.requirements import cited_test_helpers
from backend.workflow.infrastructure.sandbox import SandboxResult

_FILE = '''"""Safe Mode ledger."""

import pytest


def _row(rid, status):
    return {"id": rid, "status": status}


MIXED = [
    _row("R1", "unclear"),
    _row("R2", "done"),
    _row("R3", "deferred"),
]

UNUSED = "never shown"


@pytest.fixture
def registry():
    return make_registry()


async def _hold(db, ledger):
    """Store a real run + VerificationResult + queue item."""
    await store_real_rows(db, ledger)


def unrelated_helper():
    return "not named by the test"


async def test_only_unclear_and_deferred_are_attention(db, registry):
    await _hold(db, MIXED)
    item = await read_through_the_real_handler(registry)
    assert item["ledger_attention"] == ["R1", "R3"]
'''


def test_the_helpers_a_test_names_are_found_in_its_file() -> None:
    body = (
        "async def test_only_unclear_and_deferred_are_attention(db, registry):\n"
        "    await _hold(db, MIXED)\n"
    )
    shown = cited_test_helpers(_FILE, body)

    assert 'MIXED = [\n    _row("R1", "unclear"),' in shown
    assert '    _row("R3", "deferred"),\n]' in shown
    assert "async def _hold(db, ledger):" in shown and "store_real_rows" in shown
    assert "@pytest.fixture\ndef registry():" in shown
    assert "def _row(rid, status):" in shown  # named by MIXED, one level down
    assert "UNUSED" not in shown
    assert "unrelated_helper" not in shown


def test_helpers_are_bounded() -> None:
    big = "".join(f"def h{i}():\n" + "    x = 1\n" * 200 + "\n" for i in range(20))
    body = "def test_x():\n    " + " ".join(f"h{i}()" for i in range(20)) + "\n"
    assert len(cited_test_helpers(big + body, body, limit=2000)) <= 2000 + 200


class _Box:
    workspace_mount = "/w"

    async def exec(self, command: str, **_k: Any) -> SandboxResult:
        return SandboxResult(exit_code=0, stdout="", stderr="", timed_out=False)

    async def read_file(self, rel_path: str, max_bytes: int) -> bytes:
        return _FILE.encode()[:max_bytes]


async def test_the_judge_sees_the_fixture_and_helper_behind_a_cited_test() -> None:
    excerpt = diff_excerpt(_Box(), "abc123")  # type: ignore[arg-type]
    shown = await excerpt(
        {
            "id": "R6",
            "text": "unclear 1개·deferred 1개인 장부에 그 두 항목만 ledger_attention 으로",
            "paths": [],
            "tests": [
                "tests/mcp/test_safe_mode_ledger.py::test_only_unclear_and_deferred_are_attention"
            ],
            "note": "",
        }
    )

    assert "async def test_only_unclear_and_deferred_are_attention" in shown
    assert '_row("R3", "deferred")' in shown
    assert "store_real_rows" in shown
