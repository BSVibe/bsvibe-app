"""Where a ``client_attach`` run does its work on the founder's machine.

Not in their checkout. In a **git worktree of its own**, on its own branch, on
the same disk and sharing the same object store.

The old behaviour — edit the founder's working directory directly — produced
three failures, all observed on 2026-08-10/11:

* **Uncommitted work accumulated.** Files written by a run cancelled hours
  earlier were still sitting in the tree, and a later session read the tidy
  history as "that run produced nothing". It had produced everything.
* **Two runs could not proceed at once.** They edit the same files with no
  boundary between them.
* **The founder's own work-in-progress was in the blast radius.** No commit step
  can tell their changes from the run's when both live in one tree.

A worktree ends all three, and it is already this founder's idiom
(``bsvibe-app/wt/<branch>``).

This module is the PURE half: the path derivation and the shell commands. The
dispatch of those commands to the founder's machine lives in the sandbox layer,
which is the only thing that knows how to reach that machine.
"""

from __future__ import annotations

import shlex
import uuid

#: Worktrees live under this directory inside the product's checkout. Inside,
#: not beside, so everything about one product stays under one path — and
#: excluded locally (see :func:`worktree_provision_command`) so the founder's
#: ``git status`` does not fill with our directories.
_WORKTREE_DIR = "wt"

#: Enough of a uuid to be unambiguous in a directory listing and short enough to
#: read. Collisions across a founder's concurrent runs are not a practical risk
#: at 8 hex digits, and the run id is recoverable from the branch either way.
_SHORT = 8


def _short(run_id: uuid.UUID) -> str:
    return str(run_id)[:_SHORT]


def worktree_branch(run_id: uuid.UUID) -> str:
    """The branch this run's work lands on.

    Named for the run because that is the only durable answer to "what is this
    and can I delete it" — both for the founder reading a branch list and for
    the delivery step that later opens a PR from it.
    """
    return f"run/{_short(run_id)}"


def client_run_worktree(client_workspace_dir: str, run_id: uuid.UUID) -> str:
    """This run's own checkout path on the founder's machine.

    DERIVED, never passed around: two callers need it — the agent's dispatch
    (the CLI runs with this as its cwd) and the verification box (its commands
    and its disposable environment read from here). Threading a value from one
    to the other is a wiring dependency that can drift; a pure function of
    (repo, run) cannot.
    """
    return f"{client_workspace_dir.rstrip('/')}/{_WORKTREE_DIR}/{_short(run_id)}"


def worktree_provision_command(client_workspace_dir: str, run_id: uuid.UUID) -> str:
    """One shell command that makes this run's worktree exist, idempotently.

    ``worktree add`` on an existing path is a hard error, and runs DO re-enter
    this step (resume, retry). A run that dies here never reaches the work it
    was retrying — this codebase has already paid for that reentrancy trap once,
    with a re-run ``git clone`` stalling a run at OPEN.

    ``.git/info/exclude`` keeps the founder's ``git status`` clean. Local by
    design: BSVibe must not edit a tracked ``.gitignore`` to make room for
    itself in someone else's repo.

    Nothing here switches, stashes or resets the founder's checkout. That is the
    whole point — their branch and their work-in-progress stay exactly as they
    left them.
    """
    repo = shlex.quote(client_workspace_dir.rstrip("/"))
    path = shlex.quote(client_run_worktree(client_workspace_dir, run_id))
    branch = shlex.quote(worktree_branch(run_id))
    exclude = shlex.quote(f"/{_WORKTREE_DIR}/")
    return (
        # Prune first: a worktree directory deleted by hand leaves a stale
        # registration behind, and `add` then refuses a path it believes is
        # taken. Pruning is a no-op when there is nothing stale.
        f"git -C {repo} worktree prune; "
        f"grep -qxF -- {exclude} {repo}/.git/info/exclude 2>/dev/null "
        f"|| echo {exclude} >> {repo}/.git/info/exclude; "
        # Reuse an existing checkout rather than failing on it — the resumed run
        # wants the work it already did, not a fresh empty tree.
        f"[ -d {path} ] "
        f"|| git -C {repo} worktree add {path} -b {branch} "
        # The branch may already exist from an earlier attempt whose directory
        # was removed; then check it out instead of re-creating it.
        f"|| git -C {repo} worktree add {path} {branch}"
    )


__all__ = ["client_run_worktree", "worktree_branch", "worktree_provision_command"]
