"""Auto-ship gate — only LINKED worktrees fast-forward into the product repo.

Issue #362, safe gate fix: ``_auto_ship_product_run`` runs the local
``merge_to_main`` fast-forward. That is only valid for a **linked git worktree**
(``.git`` is the gitdir-pointer FILE, sharing the product repo's ref store). A
github-binding-provisioned run is a **standalone clone** (``.git`` is a
DIRECTORY with its own ref store + ``origin``) whose run branch is invisible to
the product repo — ``merge_to_main`` can only fail there. Those runs deliver via
the push+PR path instead, so the gate must skip them.
"""

from __future__ import annotations

from pathlib import Path

from backend.workflow.application.agent_runner import _is_linked_worktree


def test_linked_worktree_dotgit_file_is_linked(tmp_path: Path) -> None:
    # A linked worktree's `.git` is a FILE: `gitdir: /…/.git/worktrees/<name>`.
    (tmp_path / ".git").write_text("gitdir: /app/var/products/p/.git/worktrees/r\n")
    assert _is_linked_worktree(tmp_path) is True


def test_standalone_clone_dotgit_dir_is_not_linked(tmp_path: Path) -> None:
    # A standalone clone's `.git` is a DIRECTORY (own object + ref store).
    (tmp_path / ".git").mkdir()
    assert _is_linked_worktree(tmp_path) is False


def test_no_dotgit_is_not_linked(tmp_path: Path) -> None:
    # No git workspace at all (glue tests that bypass the provisioner).
    assert _is_linked_worktree(tmp_path) is False
