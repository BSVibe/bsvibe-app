"""The server_sandbox cwd is ONE fixed directory, not a fresh temp dir per task.

Nothing reads or writes that directory. Measured on the worker's own source, the
whole of what it is used for is two lines: it becomes the subprocess ``cwd``, and
it gets ``shutil.rmtree``'d — and the second exists only because of the first.
``handle_task``'s own docstring already said so: *"The dir exists only because a
CLI subprocess needs a cwd."*

Minting a UNIQUE one per task is not free, and the cost was invisible because the
cleanup looked tidy. The CLI keys per-project state on the cwd PATH, so every task
left a permanent directory behind in ``~/.claude/projects`` that the ``rmtree``
never touched. Measured on the prod worker host, 2026-09-16: **972 of 994** entries
there were ``bsvibe-task-*`` state dirs — one per task ever run, kept forever.

So: one fixed directory, created once, never deleted. These tests pin that, and
pin that ``client_attach`` (#692) is untouched by the change.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from backend.executors.worker import main as worker_main

from .test_main import _client, _task, _WorkspaceCapturingExecutor


async def _run(task: dict[str, Any], executor: Any, *, sandbox_cwd: str | None) -> dict[str, Any]:
    state: dict[str, Any] = {}
    async with _client(state) as client:
        await worker_main.handle_task(
            task,
            executors={"claude_code": executor},
            client=client,
            headers={"X-Worker-Token": "WORKER-TOKEN"},
            redis=None,
            sandbox_cwd=sandbox_cwd,
        )
    return state


async def test_two_server_sandbox_tasks_get_the_SAME_cwd(tmp_path: Path) -> None:
    """The load-bearing assertion: the cwd is reused, not minted per task.

    A fresh ``mkdtemp`` per task is what accumulates one permanent
    ``~/.claude/projects`` entry per task forever.
    """
    root = tmp_path / "cwd"
    root.mkdir()
    root = str(root)
    first, second = _WorkspaceCapturingExecutor(), _WorkspaceCapturingExecutor()

    await _run(_task(), first, sandbox_cwd=root)
    await _run(_task(), second, sandbox_cwd=root)

    assert first.seen_workspace == second.seen_workspace, (
        first.seen_workspace,
        second.seen_workspace,
    )
    assert first.workspace_existed is True
    assert second.workspace_existed is True


async def test_the_cwd_survives_the_task(tmp_path: Path) -> None:
    """It is never ``rmtree``'d — there is nothing in it to clean up."""
    root = tmp_path / "cwd"
    root.mkdir()
    root = str(root)
    executor = _WorkspaceCapturingExecutor()

    state = await _run(_task(), executor, sandbox_cwd=root)

    assert state["results"][0]["success"] is True
    assert executor.seen_workspace is not None
    assert os.path.isdir(executor.seen_workspace)


async def test_the_cwd_is_created_when_missing(tmp_path: Path) -> None:
    """First run on a fresh host must not fail for want of the directory."""
    root = tmp_path / "not" / "yet" / "there"
    assert not root.exists()

    executor = _WorkspaceCapturingExecutor()
    state = await _run(_task(), executor, sandbox_cwd=str(root))

    assert state["results"][0]["success"] is True
    assert root.is_dir()


async def test_no_per_task_directory_is_left_anywhere_under_the_root(tmp_path: Path) -> None:
    """Absence guard, pinned on the RESULT SET rather than on a name pattern.

    Asserting "no ``bsvibe-task-*`` remains" would only prove I can spell the
    prefix I happen to know. Pin the whole listing instead: after three tasks the
    root holds exactly the one fixed cwd, so ANY new per-task directory — under
    any future name — fails this.
    """
    base = tmp_path / "sandbox"  # a parent OWNED by this test (tmp_path holds
    base.mkdir()  # the subpackage conftest's home redirects too)
    root = base / "cwd"
    root.mkdir()
    for _ in range(3):
        await _run(_task(), _WorkspaceCapturingExecutor(), sandbox_cwd=str(root))

    assert root.is_dir()
    assert sorted(p.name for p in base.iterdir()) == ["cwd"]
    assert list(root.iterdir()) == []


async def test_concurrent_tasks_share_the_cwd_without_error(tmp_path: Path) -> None:
    """The one real risk of dropping per-task isolation: ``max_parallel_tasks``
    is 3, so three tasks now run against the same cwd at once.

    Nothing reads or writes the directory, so sharing it must be a non-event —
    this pins that it is, rather than assuming it.
    """
    root = tmp_path / "cwd"
    root.mkdir()
    root = str(root)
    executors = [_WorkspaceCapturingExecutor() for _ in range(3)]

    states = await asyncio.gather(*(_run(_task(), ex, sandbox_cwd=root) for ex in executors))

    assert all(s["results"][0]["success"] is True for s in states)
    seen = {ex.seen_workspace for ex in executors}
    assert len(seen) == 1, seen
    assert all(ex.workspace_existed is True for ex in executors)


async def test_client_attach_is_untouched_by_this_change(tmp_path: Path) -> None:
    """#692 regression control — the user's OWN directory is still used in place
    and still never deleted. This must stay true however the sandbox cwd works.
    """
    cwd_root = tmp_path / "cwd"
    cwd_root.mkdir()
    user_dir = tmp_path / "user-repo"
    user_dir.mkdir()
    (user_dir / "keep.txt").write_text("mine")
    executor = _WorkspaceCapturingExecutor()

    state = await _run(
        _task(workspace_dir=str(user_dir), execution_target="client_attach"),
        executor,
        sandbox_cwd=str(cwd_root),
    )

    assert state["results"][0]["success"] is True
    assert executor.seen_workspace == str(user_dir)
    assert (user_dir / "keep.txt").read_text() == "mine"
