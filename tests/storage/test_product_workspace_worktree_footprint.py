"""The change count is read from the run's worktree — what delivery will commit.

``artifact_refs`` only lists files written through the work tools, so a store swept into the
worktree (run 14e222a1: 18,752 files, 7 refs) never showed up in it.
"""

from __future__ import annotations

import uuid

import pytest

from backend.config import get_settings
from backend.storage.product_workspace import (
    add_run_worktree,
    capture_run_worktree_change,
    commit_worktree,
    init_product_workspace,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _isolate_workspace_roots(tmp_path, monkeypatch):
    monkeypatch.setattr(
        get_settings(), "product_workspace_root", str(tmp_path / "products"), raising=False
    )
    monkeypatch.setattr(get_settings(), "run_workspace_root", str(tmp_path / "runs"), raising=False)


async def test_counts_committed_and_untracked_files_but_not_ignored(tmp_path) -> None:
    product_id, run_id = uuid.uuid4(), uuid.uuid4()
    await init_product_workspace(product_id)
    worktree = await add_run_worktree(product_id, run_id)

    (worktree / "src").mkdir()
    (worktree / "src" / "a.py").write_text("a = 1\n")
    await commit_worktree(product_id, run_id, message="work: a")
    # Swept in after the commit: untracked, not ignored -> delivery would commit these.
    store = worktree / ".pnpm-store" / "v3"
    store.mkdir(parents=True)
    for i in range(30):
        (store / f"f{i}").write_text(str(i))
    (worktree / ".gitignore").write_text("ignored/\n")
    (worktree / "ignored").mkdir()
    (worktree / "ignored" / "x").write_text("x")

    change = await capture_run_worktree_change(product_id, run_id)

    assert change is not None
    assert change["changed_files"] == 32  # src/a.py + 30 store files + .gitignore
    assert change["top_dirs"][0] == {"dir": ".pnpm-store", "count": 30}


async def test_unknown_when_there_is_no_worktree() -> None:
    assert await capture_run_worktree_change(uuid.uuid4(), uuid.uuid4()) is None
