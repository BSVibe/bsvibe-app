"""Git-backed product workspace + per-run worktrees (W1).

Workflow §13 (product workspace design). Each product has a canonical git
repo at ``settings.product_workspace_root/<product_id>``; each run gets
its own git worktree on a ``bsvibe/run/<run_id>`` branch.

This module owns the *filesystem + git* lifecycle. The transactional state
(``RunStatus``, ``Decision``) lives in
:class:`~backend.orchestrator.agent_runner.AgentRunner` — this module
just shells out to git for the FS-level operations.

W1 scope (this lift):
* ``init_product_workspace`` — git init + initial commit, idempotent
* ``add_run_worktree`` / ``remove_run_worktree`` — per-run worktree
  lifecycle
* :class:`ProductWorkspaceError` — domain error for non-zero git exit

W2 scope (next lift): ``merge_to_main`` / ``merge_main_into_worktree``
(verify-time merge integration) + advisory lock. Not in this module
until W2.

Subprocess-based (no pygit2 / dulwich dependency). Every git invocation
uses ``asyncio.create_subprocess_exec`` with an argument list (NEVER
``shell=True``) so a malicious run_id / product_id can't inject shell
metacharacters. UUIDs are validated by SQLAlchemy column types upstream;
this module accepts them verbatim and trusts the type.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from pathlib import Path

import structlog

from backend.config import get_settings

logger = structlog.get_logger(__name__)

#: The author identity baked into every commit BSVibe makes inside a
#: product workspace. v1 hardcoded — per-workspace customisation is a
#: future lift. Mirrors the "we are the agent" framing: the founder is
#: the employer, BSVibe agent is the employee committing on their behalf.
_GIT_AUTHOR_NAME = "BSVibe Agent"
_GIT_AUTHOR_EMAIL = "agent@bsvibe.dev"

#: Branch prefix for every BSVibe-managed per-run branch. Keeps a future
#: github connector filter ("only show BSVibe branches") clean.
_RUN_BRANCH_PREFIX = "bsvibe/run/"

#: Relative path of the marker file committed at workspace init. Gives
#: the initial commit something to point at (an empty-tree commit is
#: legal but tooling — including ``git worktree add`` on some versions —
#: is happier with a non-empty tree).
_PRODUCT_MARKER_PATH = ".bsvibe/PRODUCT.md"


class ProductWorkspaceError(RuntimeError):
    """A git invocation against a product workspace exited non-zero.

    The ``stderr`` attribute carries the captured stderr so the caller
    (which may need to surface a human-legible reason on a Decision /
    audit row) can build a meaningful message without re-running git.
    """

    def __init__(self, message: str, *, stderr: str = "") -> None:
        super().__init__(message)
        self.stderr = stderr


@dataclass(frozen=True)
class _GitResult:
    returncode: int
    stdout: str
    stderr: str


def product_workspace_path(product_id: uuid.UUID) -> Path:
    """``var/products/<product_id>/`` — the canonical project state."""
    return Path(get_settings().product_workspace_root) / str(product_id)


def run_worktree_path(run_id: uuid.UUID) -> Path:
    """``var/runs/<run_id>/`` — the per-run worktree. Aligns with the
    existing ``run_workspace_root`` setting so the sandbox manager's
    mount layout is unchanged (sandbox already mounts this path)."""
    return Path(get_settings().run_workspace_root) / str(run_id)


def run_branch_name(run_id: uuid.UUID) -> str:
    """``bsvibe/run/<run_id>`` — the branch used by ``add_run_worktree``."""
    return f"{_RUN_BRANCH_PREFIX}{run_id}"


async def _git(
    *args: str,
    cwd: Path | None = None,
    check: bool = True,
) -> _GitResult:
    """Run ``git <args>`` and return its captured output.

    ``check=True`` raises :class:`ProductWorkspaceError` on non-zero
    exit. ``check=False`` returns the result regardless — used by
    operations that need to distinguish git's specific exit codes
    (e.g. merge returns 1 on conflict, which is NOT an error for the
    caller, only for the type of result).

    A missing ``cwd`` (e.g. caller tried ``add_run_worktree`` against
    a product that was never initialised) bubbles up as a typed
    :class:`ProductWorkspaceError` rather than the raw subprocess
    ``FileNotFoundError`` so the caller's error handling is uniform.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd) if cwd is not None else None,
        )
    except FileNotFoundError as exc:
        raise ProductWorkspaceError(
            f"git {' '.join(args)}: cwd missing ({cwd})",
            stderr=str(exc),
        ) from exc
    stdout_b, stderr_b = await proc.communicate()
    stdout = stdout_b.decode("utf-8", errors="replace")
    stderr = stderr_b.decode("utf-8", errors="replace")
    result = _GitResult(returncode=proc.returncode or 0, stdout=stdout, stderr=stderr)
    if check and result.returncode != 0:
        raise ProductWorkspaceError(
            f"git {' '.join(args)} exited {result.returncode}",
            stderr=result.stderr,
        )
    return result


async def init_product_workspace(product_id: uuid.UUID) -> None:
    """Create + initialise the product workspace if it doesn't exist.

    Idempotent: a workspace with an existing ``.git`` directory is
    detected and left alone (this is the path the startup hook takes
    for products created before W1 lift).

    Steps on a fresh workspace:

    1. ``mkdir -p var/products/<pid>``
    2. ``git init --initial-branch=main`` (suppresses git's hint about
       the default branch and locks ``main`` as the canonical branch
       name across all products regardless of operator-local config)
    3. Write ``.bsvibe/PRODUCT.md`` with product metadata
    4. ``git add . && git commit`` as the BSVibe agent author

    A failure at any step raises :class:`ProductWorkspaceError` with
    the captured git stderr. The directory may be left in a partial
    state; the caller (typically ``ProductService.create``) is
    responsible for surfacing the error.
    """
    path = product_workspace_path(product_id)
    if (path / ".git").exists():
        logger.debug("product_workspace_already_initialised", product_id=str(product_id))
        return

    path.mkdir(parents=True, exist_ok=True)
    await _git("init", "--initial-branch=main", cwd=path)
    await _configure_repo_identity(path)

    marker = path / _PRODUCT_MARKER_PATH
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        f"# BSVibe Product\n\nproduct_id: {product_id}\nworkspace: {path}\n",
        encoding="utf-8",
    )

    await _git("add", "-A", cwd=path)
    await _git(
        "commit",
        "-m",
        "initial: product workspace init",
        cwd=path,
    )
    logger.info("product_workspace_initialised", product_id=str(product_id), path=str(path))


async def _configure_repo_identity(repo: Path) -> None:
    """Set ``user.name`` / ``user.email`` at the repo level so commits
    don't fall back to a (possibly missing) global config and produce
    ``fatal: empty ident name`` on hosts where git isn't pre-configured.
    Local config wins over global, so this is safe in environments that
    already have a user identity set."""
    await _git("config", "user.name", _GIT_AUTHOR_NAME, cwd=repo)
    await _git("config", "user.email", _GIT_AUTHOR_EMAIL, cwd=repo)


async def add_run_worktree(product_id: uuid.UUID, run_id: uuid.UUID) -> Path:
    """``git worktree add var/runs/<rid> -b bsvibe/run/<rid> main``.

    Cheap branching: the new worktree shares the product workspace's
    ``.git/objects`` (no file copy). Returns the absolute worktree path
    the caller will mount into the sandbox.

    Idempotent on the worktree path: if ``var/runs/<rid>`` already
    exists and git knows about it (i.e. it's a registered worktree),
    return its path without re-running ``add``. If the dir exists but
    git doesn't know about it (a stale leftover from before W1), this
    raises ``ProductWorkspaceError`` — the caller should remove the
    stale dir first (manual operator action; we don't auto-delete
    user data).
    """
    product_path = product_workspace_path(product_id)
    worktree_path = run_worktree_path(run_id)
    branch = run_branch_name(run_id)

    if worktree_path.exists():
        # Is it a registered worktree of THIS product? Walk the worktree
        # list and look for a matching path. Cheap, exact match.
        result = await _git("worktree", "list", "--porcelain", cwd=product_path)
        if str(worktree_path.resolve()) in result.stdout:
            logger.debug(
                "run_worktree_already_exists",
                product_id=str(product_id),
                run_id=str(run_id),
            )
            return worktree_path
        raise ProductWorkspaceError(
            f"run worktree path exists but is not a registered worktree: {worktree_path}"
        )

    # ``-b`` creates the branch; we anchor it to ``main`` so the run
    # starts from the latest shipped state. Parents are created by git
    # itself when the path doesn't exist yet.
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    await _git(
        "worktree",
        "add",
        str(worktree_path),
        "-b",
        branch,
        "main",
        cwd=product_path,
    )
    logger.info(
        "run_worktree_added",
        product_id=str(product_id),
        run_id=str(run_id),
        path=str(worktree_path),
        branch=branch,
    )
    return worktree_path


async def remove_run_worktree(
    product_id: uuid.UUID,
    run_id: uuid.UUID,
    *,
    delete_branch: bool = True,
) -> None:
    """``git worktree remove`` + optionally ``git branch -D``.

    Idempotent: a missing worktree / branch is a no-op, not an error.
    This matters for the cleanup hook that fires on both ship AND
    discard — the same hook may run after a partial-cleanup retry.

    ``delete_branch=False`` lets the caller keep the branch around
    (e.g. for inspection after a ship_anyway forced merge); the default
    is to delete because a shipped/discarded run's branch is no longer
    referenced — git fast-forward already moved ``main``.
    """
    product_path = product_workspace_path(product_id)
    worktree_path = run_worktree_path(run_id)
    branch = run_branch_name(run_id)

    if worktree_path.exists():
        # ``--force`` skips the "uncommitted changes" check; on a clean
        # ship the worktree was just committed, but on a discard it may
        # carry uncommitted edits the founder explicitly threw away.
        result = await _git(
            "worktree",
            "remove",
            "--force",
            str(worktree_path),
            cwd=product_path,
            check=False,
        )
        if result.returncode != 0:
            logger.warning(
                "run_worktree_remove_failed",
                product_id=str(product_id),
                run_id=str(run_id),
                stderr=result.stderr,
            )
            # Don't raise — best-effort cleanup. The worker tick will
            # retry on the next pass. Branch deletion still attempted
            # below so a half-cleaned state can finish next time.

    if delete_branch:
        # ``-D`` (force) — the branch may have un-merged commits if the
        # run was discarded mid-flight; we explicitly want to drop them.
        result = await _git(
            "branch",
            "-D",
            branch,
            cwd=product_path,
            check=False,
        )
        if result.returncode != 0 and "not found" not in result.stderr.lower():
            logger.warning(
                "run_branch_delete_failed",
                product_id=str(product_id),
                run_id=str(run_id),
                stderr=result.stderr,
            )

    logger.info(
        "run_worktree_removed",
        product_id=str(product_id),
        run_id=str(run_id),
    )


__all__ = [
    "ProductWorkspaceError",
    "add_run_worktree",
    "init_product_workspace",
    "product_workspace_path",
    "remove_run_worktree",
    "run_branch_name",
    "run_worktree_path",
]
