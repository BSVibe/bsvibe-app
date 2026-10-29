"""A package manager's store created inside the checkout is never committable.

Prod 2026-10-28/29: the verification sandbox mounts the run's checkout on its own filesystem,
so ``pnpm install`` (the product's final check) puts its content-addressable store at the
mount root — ``<checkout>/.pnpm-store/``, ~18,750 files. The next round's ``commit_worktree``
(``git add -A``) committed it onto the run branch: R4 then saw 18,750 changed paths (one round
ended as ``system_error`` on an argv too long; another skipped), and the PR would have carried
the whole store.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize(
    "path",
    [
        ".pnpm-store/v10/files/00/abc",
        "apps/pwa/.pnpm-store/v10/index/x.json",
    ],
)
def test_a_pnpm_store_inside_the_checkout_is_ignored(path: str) -> None:
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", "-q", path],
        cwd=REPO_ROOT,
        check=False,
    )
    assert result.returncode == 0, f"{path} would be committed by `git add -A`"
