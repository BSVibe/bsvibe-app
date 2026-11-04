"""The judge reading a cited test also reads the helpers it imports from another file.

Prod #1302 (run f529b012, 2026-11-04): R5 ("real runs, cards and route handlers") came back
``unclear``. The cited test seeded its rows through ``issue_t._seed`` / ``pr_t._seed`` —
helpers imported from two other test modules — and #1289 shows only same-file helpers, so
the judge could not see what was stored.

Now a name the test uses through an import (``mod.name`` on an imported module, or a name
imported from a module) is looked up in that module's file, Python and JS/TS alike.
"""

from __future__ import annotations

from typing import Any

from backend.workflow.application.requirements_ledger import diff_excerpt
from backend.workflow.domain.requirements import imported_helper_sources
from backend.workflow.infrastructure.sandbox import SandboxResult

_PY_TEST = """from tests.workflow.application import test_issue_edit_routing as issue_t
from .helpers import make_card


async def test_reopen_denies_pending_card() -> None:
    ws, run_id = await issue_t._seed(sf, status=status)
    card = await make_card(ws, run_id)
    assert card.status == "denied"
"""

_ROUTING = '''import uuid


async def _seed(sf, *, status):
    """A run started from the issue's opened delivery, with its ledger recorded."""
    STORED_A_REAL_RUN = True
    return uuid.uuid4(), uuid.uuid4()


def _event(body):
    return {}
'''

_HELPERS = """def make_card(ws, run_id):
    return REAL_SAFE_MODE_ROW
"""


def test_python_imports_point_at_the_files_that_define_the_names() -> None:
    body = _PY_TEST[_PY_TEST.index("async def") :]
    wanted = imported_helper_sources(_PY_TEST, "tests/workflow/application/test_x.py", body)

    assert ("tests/workflow/application/test_issue_edit_routing.py",) in [w[0] for w in wanted]
    assert any(name == "_seed" for _paths, name in wanted)
    assert any(
        "tests/workflow/application/helpers.py" in paths and name == "make_card"
        for paths, name in wanted
    )
    assert not any(name == "_event" for _paths, name in wanted)  # imported, never used


def test_ts_imports_point_at_the_files_that_define_the_names() -> None:
    text = (
        'import { seedRun, other as renamed } from "./support";\n'
        'import * as api from "../lib/api";\n'
        'it("shows the card", async () => {\n'
        "  const run = await seedRun();\n"
        "  await api.listCards(run);\n"
        "});\n"
    )
    body = text[text.index("it(") :]  # what the judge is shown: the test, not the imports
    wanted = dict(
        (name, paths)
        for paths, name in imported_helper_sources(text, "apps/pwa/test/card.test.tsx", body)
    )

    assert "apps/pwa/test/support.ts" in wanted["seedRun"]
    assert "apps/pwa/lib/api.ts" in wanted["listCards"]
    assert "other" not in wanted  # imported as ``renamed``, never used


class _Box:
    workspace_mount = "/w"
    files = {
        "tests/workflow/application/test_reopen.py": _PY_TEST,
        "tests/workflow/application/test_issue_edit_routing.py": _ROUTING,
        "tests/workflow/application/helpers.py": _HELPERS,
    }

    async def exec(self, command: str, **_k: Any) -> SandboxResult:
        return SandboxResult(exit_code=0, stdout="", stderr="", timed_out=False)

    async def read_file(self, rel_path: str, max_bytes: int) -> bytes:
        if rel_path not in self.files:
            raise FileNotFoundError(rel_path)
        return self.files[rel_path].encode()[:max_bytes]


async def test_the_judge_sees_helpers_imported_from_other_files() -> None:
    excerpt = diff_excerpt(_Box(), "abc123")  # type: ignore[arg-type]
    shown = await excerpt(
        {
            "id": "R5",
            "text": "실제 런·대기 카드를 저장하고 실제 핸들러로",
            "paths": [],
            "tests": ["tests/workflow/application/test_reopen.py::test_reopen_denies_pending_card"],
            "note": "",
        }
    )

    assert "STORED_A_REAL_RUN" in shown
    assert "REAL_SAFE_MODE_ROW" in shown
    assert "defined in tests/workflow/application/test_issue_edit_routing.py" in shown
    assert "def _event" not in shown
