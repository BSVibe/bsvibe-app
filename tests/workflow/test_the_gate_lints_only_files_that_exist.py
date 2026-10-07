"""A file the run created and then removed is not a file to lint.

Prod run ``4414bcd5`` (2026-10-07, #1073 handed to BSVibe as a measurement):
the agent wrote a scratch helper ``_patch_engine.py`` at the repo root, used it,
and removed it through the shell. ``written_paths`` only ever GROWS — it records
every ``file_write`` and never learns of a deletion — and the gate deriver is
handed it as "changed files", so it derived ``ruff check … _patch_engine.py``.
Ruff answered E902 (no such file). That failure was nobody's to fix: the agent
could not make the gate stop naming a file that does not exist. Two verify
rounds failed on it, then the run stopped to ask 형님 — 358k input tokens in.

The server-side deriver is now handed only the changed paths still present in
the tree. (The in-place path is left alone: its changed list comes from git on
the founder's machine, where a created-then-removed file never appears, and each
existence check there would be a task round-trip.)
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from tests.workflow.test_gate_deriver_sees_repo_ci import (
    _Box,
    _Llm,
    _real_worktree,
    _Run,
    _service,
)

pytestmark = pytest.mark.asyncio

_GATE_JSON = json.dumps({"applicable": True, "commands": [{"command": "true", "kind": "quality"}]})
_TREE = {"pyproject.toml": "[project]\nname = 'x'\n", "money.py": "X = 1\n"}


async def test_the_server_side_gate_is_not_derived_over_a_removed_file(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    llm = _Llm(_GATE_JSON)
    run = _Run()
    _real_worktree(monkeypatch, tmp_path, run)

    await _service(llm)._run_derived_gate(run, _Box(_TREE), ["money.py", "_patch_engine.py"])

    assert "money.py" in llm.prompt
    assert "_patch_engine.py" not in llm.prompt


async def test_every_present_file_still_reaches_the_deriver(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Control — nothing removed, nothing dropped."""
    llm = _Llm(_GATE_JSON)
    run = _Run()
    _real_worktree(monkeypatch, tmp_path, run)

    await _service(llm)._run_derived_gate(run, _Box(_TREE), ["money.py", "pyproject.toml"])

    assert "money.py" in llm.prompt
    assert "pyproject.toml" in llm.prompt
