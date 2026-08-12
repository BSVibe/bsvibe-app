"""Keeping a watched PR mergeable — on whichever machine holds its checkout.

The merge-watch worker keeps a PR mergeable by merging the base branch into the
run branch and pushing the result. Until now it did that in a server-side clone,
which is exactly what a ``client_attach`` product must never have (§3.5: the
source lives only on the founder's machine). So #738 stopped there: the resolver
returned ``None``, and such a PR waited for a human who was never told.

The stop was right and the conclusion was too wide — the same shape #692 already
corrected once for the verification gate. *Where* the command runs was never the
question. ``git merge`` decides clean-vs-conflict by its exit code, and that
answer is identical on any machine holding the checkout. The founder's machine
holds it, the exec channel reaches it (#702), and the run's branch and worktree
are already named there (#734).

So the freshen is a CAPABILITY the worker asks for, not a block of git it owns,
and each execution model supplies its own — the seam shape the sandbox (#692)
and the check environment (#730) already use. The worker keeps every decision
that follows git's answer; only the hands change.

What does NOT change, in either model:

* a conflict is git's verdict, never a model's;
* a failure is a failure — never a quiet fall back to the other model's machine,
  which for client_attach would mean cloning their source here to fix a
  mergeability problem ([[bsvibe-no-implicit-routing]]);
* no server-held credential and no source travel to the founder's machine. Their
  own git credential does the push, exactly as #735's commit does.
"""

from __future__ import annotations

import shlex
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.workflow.application.runtime.merge_watch_server_freshen import (
    build_server_freshener,
    product_id_for_run,
)
from backend.workflow.infrastructure.workers.merge_watch_worker import FreshenOutcome

logger = structlog.get_logger(__name__)

#: Local git plumbing on the founder's machine — a merge, not a build.
_GIT_TIMEOUT_S = 120.0
#: A fetch/push crosses the network to github.
_NETWORK_TIMEOUT_S = 300.0

#: A box that can run commands in one run's checkout, or ``None`` when that
#: machine cannot be reached at all.
BoxFactory = Callable[[uuid.UUID], Awaitable[Any]]


async def _run(box: Any, command: str, *, timeout_s: float) -> tuple[bool, str]:
    """Run one git command in the run's worktree; ``(ok, output)``."""
    try:
        # S604: the sandbox Protocol's own flag, not a subprocess call — this box
        # dispatches the string to the founder's machine as an exec task.
        res = await box.exec(command, timeout_s=timeout_s, shell=True)  # noqa: S604
    except Exception as exc:  # noqa: BLE001 — an unreachable machine is a soft failure
        return False, f"{type(exc).__name__}: {exc}"
    output = "\n".join(o for o in (res.stdout, res.stderr) if o)[-500:]
    if res.timed_out:
        return False, f"timed out after {timeout_s}s: {output}"
    return res.exit_code == 0, output


async def freshen_on_client_machine(*, box: Any, branch: str, base_branch: str) -> FreshenOutcome:
    """Merge ``origin/<base_branch>`` into ``branch`` on the founder's machine.

    The command sequence is deliberately the same one the server-side freshen
    runs, minus everything that only made sense there: no clone (the checkout
    exists), no ``--unshallow`` (it is a full repo), no token (their credential
    is already configured, and sending ours would put a server-held secret on
    someone else's disk).

    A conflict names its files from git's OWN unmerged listing rather than by
    parsing merge chatter, then **aborts** — the worktree is a reclaimable
    resource on someone else's machine (#736), and a tree left mid-merge is one
    the reaper will correctly refuse forever. The agent re-does the merge itself
    when it is re-dispatched, so nothing is lost by aborting.
    """
    ok, out = await _run(
        box, f"git fetch origin {shlex.quote(base_branch)}", timeout_s=_NETWORK_TIMEOUT_S
    )
    if not ok:
        logger.warning("freshen_in_place_fetch_failed", branch=branch, error=out)
        return FreshenOutcome(status="failed", base_branch=base_branch)

    merged, out = await _run(
        box, f"git merge origin/{shlex.quote(base_branch)}", timeout_s=_GIT_TIMEOUT_S
    )
    if not merged:
        # Unmerged paths — git's own answer, empty when the merge failed for
        # some other reason (which is then reported as a failure, not a
        # conflict: asking the agent to resolve nothing burns a whole run).
        _, listing = await _run(
            box, "git diff --name-only --diff-filter=U", timeout_s=_GIT_TIMEOUT_S
        )
        paths = tuple(p.strip() for p in listing.splitlines() if p.strip())
        await _run(box, "git merge --abort", timeout_s=_GIT_TIMEOUT_S)
        if not paths:
            logger.warning("freshen_in_place_merge_failed", branch=branch, error=out)
            return FreshenOutcome(status="failed", base_branch=base_branch)
        logger.info("freshen_in_place_conflict", branch=branch, conflict_paths=list(paths))
        return FreshenOutcome(status="conflict", base_branch=base_branch, conflict_paths=paths)

    ok, out = await _run(
        box, f"git push origin {shlex.quote(branch)}", timeout_s=_NETWORK_TIMEOUT_S
    )
    if not ok:
        # The merge is committed locally but github never saw it. Reporting
        # "clean" here would tell the worker to wait for CI on a head that does
        # not exist there — the PR would sit pending_ci forever.
        logger.warning("freshen_in_place_push_failed", branch=branch, error=out)
        return FreshenOutcome(status="failed", base_branch=base_branch)

    logger.info("freshen_in_place_freshened", branch=branch, base_branch=base_branch)
    return FreshenOutcome(status="clean", base_branch=base_branch)


def client_machine_freshener(
    *, box_factory: BoxFactory
) -> Callable[..., Awaitable[FreshenOutcome]]:
    """Bind :func:`freshen_on_client_machine` to a way of reaching that machine.

    ``box_factory`` returning ``None`` means the machine cannot be reached at
    all (no live worker, no dispatch context, no declared workspace path). That
    is a FAILURE, never a fall back to the server-side freshen: the whole reason
    this path exists is that the other one would clone their source here.
    """

    async def _freshen(run_id: uuid.UUID, *, branch: str, base_branch: str) -> FreshenOutcome:
        box = await box_factory(run_id)
        if box is None:
            logger.warning("freshen_in_place_unreachable", run_id=str(run_id), branch=branch)
            return FreshenOutcome(status="failed", base_branch=base_branch)
        return await freshen_on_client_machine(box=box, branch=branch, base_branch=base_branch)

    return _freshen


def build_branch_freshener(
    *,
    cipher: Any,
    session_factory: async_sessionmaker[AsyncSession],
    redis_client: Any,
    run_workspace_root: Path,
    git_ops: Any = None,
) -> Callable[[AsyncSession, uuid.UUID, uuid.UUID, str], Awaitable[FreshenOutcome]]:
    """Pick the freshener for THIS run: whichever machine holds its checkout.

    That is the only decision here — everything else about a freshen is
    identical across the two execution models, which is why they share the
    worker's state machine rather than each growing their own.
    """
    server = build_server_freshener(
        cipher=cipher, run_workspace_root=run_workspace_root, git_ops=git_ops
    )

    async def _freshen(
        session: AsyncSession, workspace_id: uuid.UUID, run_id: uuid.UUID, branch: str
    ) -> FreshenOutcome:
        from backend.workflow.application.delivery.connector_dispatch._resolver import (  # noqa: PLC0415
            product_runs_in_place,
        )
        from backend.workflow.application.runtime.merge_watch_client_box import (  # noqa: PLC0415
            client_box_factory,
            resolve_base_branch,
        )

        product_id = await product_id_for_run(session, run_id)
        if not await product_runs_in_place(session, product_id):
            return await server(session, workspace_id, run_id, branch)

        base_branch = await resolve_base_branch(
            session, workspace_id=workspace_id, product_id=product_id, cipher=cipher
        )
        if base_branch is None:
            logger.warning(
                "freshen_in_place_no_binding", workspace_id=str(workspace_id), run_id=str(run_id)
            )
            return FreshenOutcome(status="unavailable", base_branch="")
        freshen = client_machine_freshener(
            box_factory=client_box_factory(
                session_factory=session_factory,
                redis_client=redis_client,
                workspace_id=workspace_id,
                product_id=product_id,
            )
        )
        return await freshen(run_id, branch=branch, base_branch=base_branch)

    return _freshen


__all__ = [
    "build_branch_freshener",
    "client_machine_freshener",
    "freshen_on_client_machine",
]
