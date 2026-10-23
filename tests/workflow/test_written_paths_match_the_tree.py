"""#1145 — what a run reports as changed is what git sees changed, not a log of tool calls.

``written_paths`` only grew: every ``file_write``/``file_edit`` appended, nothing ever removed.
Measured 2026-10-07 on two runs fixing #1073:

* ``b2ebd3c4`` wrote ``_patch_engine.py``/``_patch_ar.py``/``_patch_rt.py`` at the repo root,
  ran them through ``shell_exec`` and deleted them. The deliverable's ``artifact_refs`` (the
  PR's changed-file list, settle tags, the proof view's file whitelist) listed those three
  deleted scripts — and MISSED the four source files the scripts actually edited, because a
  shell edit never passes through the write tools.
* ``4414bcd5`` did the same and the gate derived ``ruff check … _patch_engine.py`` on a file
  that was gone (the gate side was patched by #1141, the list itself was not).

So when the agent says it is done, the list is re-read from the tree: every tracked change git
sees since the run's baseline, plus the untracked files a write tool made that still exist.
Git's own untracked list is not trusted wholesale — it would bring in whatever build noise a
repo does not ignore. When git cannot answer, the list stays as it was — never emptied by a
failed probe.
"""

from __future__ import annotations

import inspect
import subprocess
import uuid
from pathlib import Path

import pytest

from backend.workflow.application.inplace_gate import capture_inplace_baseline
from backend.workflow.infrastructure.sandbox import NoopSandboxManager


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)  # noqa: S603, S607


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "engine.py").write_text("x = 1\n")
    (tmp_path / "resolver.py").write_text("y = 1\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "init")
    return tmp_path


async def test_a_deleted_scratch_file_leaves_and_a_shell_edit_arrives(repo: Path) -> None:
    from backend.workflow.application.inplace_gate import reconcile_written_paths

    box = await NoopSandboxManager().acquire(uuid.uuid4(), str(repo))
    baseline = await capture_inplace_baseline(box)
    # The run: a tool write, a scratch script that edits engine.py and is then removed.
    (repo / "resolver.py").write_text("y = 2\n")
    (repo / "_patch_engine.py").write_text("...")
    (repo / "engine.py").write_text("x = 2\n")
    (repo / "_patch_engine.py").unlink()
    written = ["resolver.py", "_patch_engine.py"]

    assert await reconcile_written_paths(box, baseline, written) == ["engine.py", "resolver.py"]


async def test_committed_work_since_the_baseline_counts(repo: Path) -> None:
    from backend.workflow.application.inplace_gate import reconcile_written_paths

    box = await NoopSandboxManager().acquire(uuid.uuid4(), str(repo))
    baseline = await capture_inplace_baseline(box)
    (repo / "engine.py").write_text("x = 3\n")
    _git(repo, "commit", "-qam", "agent commit")
    (repo / "new_test.py").write_text("def test(): pass\n")

    got = await reconcile_written_paths(box, baseline, ["new_test.py"])

    assert got == ["engine.py", "new_test.py"]


async def test_untracked_build_noise_is_not_a_change(repo: Path) -> None:
    """A repo with no ``.gitignore`` shows ``__pycache__`` as untracked — not the run's work."""
    from backend.workflow.application.inplace_gate import reconcile_written_paths

    box = await NoopSandboxManager().acquire(uuid.uuid4(), str(repo))
    baseline = await capture_inplace_baseline(box)
    (repo / "__pycache__").mkdir()
    (repo / "__pycache__" / "engine.cpython-312.pyc").write_bytes(b"\0")
    (repo / "engine.py").write_text("x = 4\n")

    assert await reconcile_written_paths(box, baseline, ["engine.py"]) == ["engine.py"]


async def test_when_git_cannot_answer_the_list_is_kept(tmp_path: Path) -> None:
    """Control — not a git tree (or an unreachable box): never empty the list on a failed probe."""
    from backend.workflow.application.inplace_gate import reconcile_written_paths

    box = await NoopSandboxManager().acquire(uuid.uuid4(), str(tmp_path))
    written = ["a.py", "b.py"]

    assert await reconcile_written_paths(box, None, written) == ["a.py", "b.py"]


async def test_a_run_that_changed_nothing_reports_nothing(repo: Path) -> None:
    from backend.workflow.application.inplace_gate import reconcile_written_paths

    box = await NoopSandboxManager().acquire(uuid.uuid4(), str(repo))
    baseline = await capture_inplace_baseline(box)
    (repo / "_scratch.py").write_text("...")
    (repo / "_scratch.py").unlink()

    assert await reconcile_written_paths(box, baseline, ["_scratch.py"]) == []


def test_the_drive_loop_reconciles_before_it_verifies() -> None:
    """The helper alone proves nothing if the loop never calls it — and the call must sit
    before the contract is assembled, whose inputs (and everything after) read the list."""
    from backend.workflow.application import _drive_loop

    src = inspect.getsource(_drive_loop.drive_loop)
    call = "written_paths[:] = await reconcile_written_paths(box, round_baseline, written_paths)"
    assert call in src
    assert src.index(call) < src.index("orch._assemble_contract(")


def test_the_mcp_edit_tool_states_its_rules() -> None:
    """The executor sees only the MCP description. It said "Replace an exact string" and
    nothing else — not that the file must be read through the tool first, nor that the match
    must be unique — so an agent that read with ``cat`` was refused for a rule it was never
    told, and editing through a throwaway script got around every rule at once."""
    from backend.mcp.tools.work_tools import _FORWARDING_PRESENTATION

    description = _FORWARDING_PRESENTATION["file_edit"][1]
    assert "bsvibe_work_file_read" in description
    assert "UNIQUE" in description
    assert "replace_all" in description
    assert "script" in description


async def test_the_read_first_refusal_says_a_shell_read_does_not_count(tmp_path: Path) -> None:
    from backend.workflow.infrastructure.tools import ToolError, ToolRegistry

    (tmp_path / "a.py").write_text("x = 1\n")
    registry = ToolRegistry(workspace_dir=tmp_path)
    registry.declared_contract = {"checks": [{"kind": "shell", "command": "true"}]}

    with pytest.raises(ToolError, match="shell_exec"):
        await registry._file_edit({"path": "a.py", "old_string": "x = 1", "new_string": "x = 2"})
