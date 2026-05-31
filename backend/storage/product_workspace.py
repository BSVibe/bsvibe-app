"""Git-backed product workspace + per-run worktrees (W1+W2).

Workflow §13 (product workspace design). Each product has a canonical git
repo at ``settings.product_workspace_root/<product_id>``; each run gets
its own git worktree on a ``bsvibe/run/<run_id>`` branch.

This module owns the *filesystem + git* lifecycle. The transactional state
(``RunStatus``, ``Decision``) lives in
:class:`~backend.workflow.application.agent_runner.AgentRunner` — this module
just shells out to git for the FS-level operations.

W1 scope:
* ``init_product_workspace`` — git init + initial commit, idempotent
* ``add_run_worktree`` / ``remove_run_worktree`` — per-run worktree
  lifecycle
* :class:`ProductWorkspaceError` — domain error for non-zero git exit

W2 additions:
* ``commit_worktree`` — stage agent's writes as a real branch commit
* ``merge_main_into_worktree`` — at verify time, pull main into the
  run worktree to detect conflicts before ship
* ``merge_to_main`` — fast-forward main onto the run branch at ship,
  guarded by advisory lock
* ``force_merge_theirs`` — executor B2b ship_anyway path (run wins)
* :class:`MergeOutcome` — typed result carrying conflict paths

Subprocess-based (no pygit2 / dulwich dependency). Every git invocation
uses ``asyncio.create_subprocess_exec`` with an argument list (NEVER
``shell=True``) so a malicious run_id / product_id can't inject shell
metacharacters. UUIDs are validated by SQLAlchemy column types upstream;
this module accepts them verbatim and trusts the type.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import get_settings
from backend.workflow.infrastructure.advisory_lock import (
    release_run_dispatch_lock,
    try_run_dispatch_lock,
)

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
    """``var/products/<product_id>/`` — the canonical project state.

    Always returns an absolute path so the subprocess ``cwd`` for ``git``
    is unambiguous. uvloop (production) interprets a relative ``cwd`` on
    ``asyncio.create_subprocess_exec`` differently from the stock asyncio
    selector loop and surfaces a bare ``FileNotFoundError`` even when the
    dir exists relative to the worker's working directory. Resolving up
    front keeps the behaviour identical across event loops.
    """
    root = Path(get_settings().product_workspace_root)
    if not root.is_absolute():
        root = Path.cwd() / root
    return root / str(product_id)


def run_worktree_path(run_id: uuid.UUID) -> Path:
    """``var/runs/<run_id>/`` — the per-run worktree. Aligns with the
    existing ``run_workspace_root`` setting so the sandbox manager's
    mount layout is unchanged (sandbox already mounts this path).

    Returns an absolute path for the same uvloop-cwd reason as
    :func:`product_workspace_path`."""
    root = Path(get_settings().run_workspace_root)
    if not root.is_absolute():
        root = Path.cwd() / root
    return root / str(run_id)


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


# ---------------------------------------------------------------------------
# W2 — verify-time merge + ship merge
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MergeOutcome:
    """Result of an attempted merge.

    ``status`` is ``"clean"`` when git completed the merge without
    conflicts (HEAD now points at the merged state) and ``"conflict"``
    when at least one path conflicts (the working tree is in a mid-merge
    state with conflict markers — caller decides whether to abort or to
    leave the markers for the agent to resolve).
    """

    status: Literal["clean", "conflict"]
    conflict_paths: list[str] = field(default_factory=list)


async def commit_worktree(
    product_id: uuid.UUID,
    run_id: uuid.UUID,
    *,
    message: str,
) -> str | None:
    """``git add -A && git commit`` in the run worktree.

    Stages every agent-written file as a real commit on the run's
    branch — so the subsequent merge into main has a clear set of
    changes to reconcile. Returns the new commit SHA on success.

    Idempotent for the "nothing to commit" case (``returncode != 0``
    from ``git commit`` with an empty index returns ``None`` without
    raising). Other failures raise :class:`ProductWorkspaceError`.
    """
    worktree = run_worktree_path(run_id)
    await _git("add", "-A", cwd=worktree)
    # ``--allow-empty`` would mask the "nothing changed" case which we
    # want to treat as a benign no-op — detect via status check first.
    status = await _git("status", "--porcelain", cwd=worktree)
    if not status.stdout.strip():
        # No staged changes — the agent didn't write anything in this
        # round. Nothing to commit; return ``None`` so callers can
        # short-circuit (e.g. verify still calls this, finds nothing,
        # moves on to merge attempt which will be a no-op too).
        return None
    await _git("commit", "-m", message, cwd=worktree)
    head = await _git("rev-parse", "HEAD", cwd=worktree)
    return head.stdout.strip()


async def merge_main_into_worktree(product_id: uuid.UUID, run_id: uuid.UUID) -> MergeOutcome:
    """Pull ``main`` into the run worktree.

    Detects pre-ship conflicts: if ``main`` has moved since the worktree
    branched (e.g. a parallel run already shipped to main), git tries to
    merge those changes into the agent's branch. Clean → run is now
    fast-forwardable; conflict → the worktree carries conflict markers
    and the agent gets another loop round to resolve them.

    Does NOT abort on conflict — leaves the worktree in the mid-merge
    state so the agent's next round (or the caller's explicit recovery)
    sees the markers via standard file_read/file_edit tools. This
    matches the Claude Code-style "agent fixes its own merge" model.

    Returns :class:`MergeOutcome` with ``status="clean"`` and empty
    paths on success; ``status="conflict"`` with the unmerged paths on
    conflict. Non-conflict git errors raise :class:`ProductWorkspaceError`.
    """
    worktree = run_worktree_path(run_id)
    # ``--no-ff`` forces a merge commit even when main hasn't moved; this
    # keeps the run-branch history honest (it always carries an explicit
    # "merged main at <SHA>" anchor). Cleaner audit at the cost of one
    # extra commit per round.
    result = await _git(
        "merge",
        "--no-ff",
        "--no-edit",
        "main",
        cwd=worktree,
        check=False,
    )
    if result.returncode == 0:
        return MergeOutcome(status="clean")
    # git exits 1 on conflicts. Capture the unmerged paths for the
    # caller to surface to the agent.
    unmerged = await _git(
        "diff",
        "--name-only",
        "--diff-filter=U",
        cwd=worktree,
        check=False,
    )
    if result.returncode == 1 and unmerged.returncode == 0:
        paths = [p for p in unmerged.stdout.splitlines() if p.strip()]
        logger.info(
            "merge_main_into_worktree_conflict",
            product_id=str(product_id),
            run_id=str(run_id),
            conflict_paths=paths,
        )
        return MergeOutcome(status="conflict", conflict_paths=paths)
    # Any other exit code (e.g. 128 on FS errors) is not a merge result.
    raise ProductWorkspaceError(f"git merge main exited {result.returncode}", stderr=result.stderr)


async def merge_to_main(product_id: uuid.UUID, run_id: uuid.UUID) -> str:
    """Fast-forward ``main`` onto the run branch.

    Pre-conditions: the run worktree just committed (so its branch
    head is up to date) AND a previous ``merge_main_into_worktree``
    returned clean (so main is an ancestor of the run branch — the
    fast-forward is guaranteed). Callers must hold the
    :func:`product_workspace_lock` for this product around the
    verify→ship sequence so no parallel run can move main between
    the worktree's merge and this fast-forward.

    Returns the new ``main`` SHA. Raises :class:`ProductWorkspaceError`
    if fast-forward is not possible (e.g. main moved while the lock was
    held by a different process — shouldn't happen with proper locking
    but surfaces if it does).
    """
    product_path = product_workspace_path(product_id)
    branch = run_branch_name(run_id)
    await _git("merge", "--ff-only", branch, cwd=product_path)
    head = await _git("rev-parse", "HEAD", cwd=product_path)
    sha = head.stdout.strip()
    logger.info(
        "merge_to_main",
        product_id=str(product_id),
        run_id=str(run_id),
        main_sha=sha,
    )
    return sha


async def force_merge_theirs(product_id: uuid.UUID, run_id: uuid.UUID) -> str:
    """``git merge -X theirs <branch>`` from product main.

    Executor B2b ``ship_anyway`` path. The founder pressed "Approve &
    ship" on a run that hit verification trouble (merge conflict or
    contract fail); we honor that intent by overriding main with the
    run's version on every conflicting path. Non-conflicting paths
    merge normally.

    ``-X theirs`` is git's strategy option, NOT a separate strategy —
    it pairs with the default ``recursive`` strategy. "Theirs" here
    means "the branch being merged IN" (= the run branch), so the run's
    edits win. The naming is git's; we surface it under the more
    user-friendly ``force_merge_theirs`` here.

    Returns the new main SHA. Use ONLY for explicit founder-overridden
    ship paths — the agent's auto-merge path goes through ``merge_to_main``.
    """
    product_path = product_workspace_path(product_id)
    branch = run_branch_name(run_id)
    await _git(
        "merge",
        "-X",
        "theirs",
        "--no-edit",
        "--no-ff",
        branch,
        cwd=product_path,
    )
    head = await _git("rev-parse", "HEAD", cwd=product_path)
    sha = head.stdout.strip()
    logger.info(
        "force_merge_theirs",
        product_id=str(product_id),
        run_id=str(run_id),
        main_sha=sha,
    )
    return sha


async def abort_merge(product_id: uuid.UUID, run_id: uuid.UUID | None = None) -> None:
    """``git merge --abort`` in either the product main (when ``run_id``
    is ``None``) or the run worktree. Best-effort cleanup; a "no merge
    in progress" exit is treated as success."""
    target = product_workspace_path(product_id) if run_id is None else run_worktree_path(run_id)
    await _git("merge", "--abort", cwd=target, check=False)


@asynccontextmanager
async def product_workspace_lock(
    session: AsyncSession, product_id: uuid.UUID
) -> AsyncIterator[None]:
    """Serialize verify→ship sequences on the same product workspace.

    Reuses the same Postgres ``pg_try_advisory_lock`` primitive
    :mod:`backend.execution.advisory_lock` already uses for run-dispatch
    locking — the lock key is derived from the *product_id*, so it's a
    different lock from the run-dispatch ones (no conflict). When two
    sessions race for the same product, the loser raises
    :class:`ProductWorkspaceBusy` and the caller (typically the
    verifier) retries on the next AgentWorker tick.

    On SQLite (unit tests), the underlying helper falls back to a
    per-process ``asyncio.Lock`` keyed by the same UUID — semantics
    match well enough for the test tier.
    """
    acquired = await try_run_dispatch_lock(session, product_id)
    if not acquired:
        raise ProductWorkspaceBusy(f"product workspace {product_id} is busy with another ship")
    try:
        yield
    finally:
        await release_run_dispatch_lock(session, product_id)


class ProductWorkspaceBusy(ProductWorkspaceError):
    """The product workspace lock is held by another session.

    Raised by :func:`product_workspace_lock` on the loser path. The
    caller is expected to retry on the next worker tick rather than
    block — long-running merges on a single product workspace should
    serialize, not pile up.
    """


@dataclass(frozen=True, slots=True)
class TreeEntry:
    """One immediate child in a product's ``main`` tree.

    ``path`` is the full repo-relative path (e.g. ``src/app.py``); ``name`` is
    the leaf (``app.py``). ``kind`` is ``"file"`` (git blob) or ``"dir"`` (git
    tree). Submodules / other object types are skipped by the lister."""

    name: str
    path: str
    kind: Literal["file", "dir"]


def _is_safe_subdir(subdir: str) -> bool:
    """A listable subdir must be repo-relative and stay inside the repo: no
    absolute paths, no ``..`` or empty segments. ``""`` (root) is always safe."""
    if subdir == "":
        return True
    if subdir.startswith("/"):
        return False
    return all(part not in ("", "..") for part in subdir.split("/"))


async def list_product_tree(product_id: uuid.UUID, subdir: str = "") -> list[TreeEntry]:
    """List the IMMEDIATE children of ``subdir`` in the product's ``main`` tree.

    One level only (lazy) — a tree browser fetches each directory on demand
    rather than walking a whole (potentially huge) repo up front. Directories
    sort before files, then by name. An unsafe ``subdir`` (absolute / ``..``)
    or a path that isn't a directory in ``main`` returns ``[]`` (never raises
    into the read path). An uninitialised product also returns ``[]``."""
    subdir = subdir.strip("/")
    if not _is_safe_subdir(subdir):
        return []
    repo = product_workspace_path(product_id)
    if not (repo / ".git").exists():
        return []
    # ``git ls-tree main -- <subdir>/`` lists one level. The trailing slash
    # scopes to the directory's children; an empty subdir lists the root.
    spec = f"{subdir}/" if subdir else ""
    args = ["ls-tree", "-z", "main"]
    if spec:
        args += ["--", spec]
    result = await _git(*args, cwd=repo, check=False)
    if result.returncode != 0:
        return []
    entries: list[TreeEntry] = []
    for record in result.stdout.split("\0"):
        if not record:
            continue
        # Format: "<mode> SP <type> SP <sha>\t<path>"
        meta, _, path = record.partition("\t")
        if not path:
            continue
        fields = meta.split(" ")
        if len(fields) < 2:
            continue
        obj_type = fields[1]
        if obj_type == "blob":
            kind: Literal["file", "dir"] = "file"
        elif obj_type == "tree":
            kind = "dir"
        else:
            continue  # submodule / other — skip
        entries.append(
            TreeEntry(name=path.rstrip("/").split("/")[-1], path=path.rstrip("/"), kind=kind)
        )
    entries.sort(key=lambda e: (e.kind != "dir", e.name.lower()))
    return entries


__all__ = [
    "MergeOutcome",
    "ProductWorkspaceBusy",
    "ProductWorkspaceError",
    "TreeEntry",
    "list_product_tree",
    "abort_merge",
    "add_run_worktree",
    "commit_worktree",
    "force_merge_theirs",
    "init_product_workspace",
    "merge_main_into_worktree",
    "merge_to_main",
    "product_workspace_lock",
    "product_workspace_path",
    "remove_run_worktree",
    "run_branch_name",
    "run_worktree_path",
]
