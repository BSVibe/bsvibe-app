"""The server-side half of keeping a watched PR mergeable.

Lifted verbatim in behaviour out of :class:`~backend.workflow.infrastructure.
workers.merge_watch_worker.MergeWatchWorker`, where it sat as a block of git the
infrastructure worker owned. It moves here because *which machine holds the
checkout* is an application question (it follows from the product's execution
model), and because the worker's state machine is identical either way — see
:mod:`backend.workflow.application.runtime.merge_watch_freshen`.

Nothing about the server path changed: same binding resolution, same re-clone of
a reaped run workspace, same ``fetch --unshallow`` + ``merge`` + ``push``.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from backend.connectors.auth.resolve import resolve_connector_credentials
from backend.workflow.application.delivery.connector_dispatch._github import github_remote_url
from backend.workflow.application.delivery.connector_dispatch._resolver import (
    resolve_github_binding,
)
from backend.workflow.infrastructure.delivery.git_ops import GitError, GitOps
from backend.workflow.infrastructure.workers.merge_watch_worker import FreshenOutcome

logger = structlog.get_logger(__name__)


@dataclass(slots=True, frozen=True)
class FreshnessTarget:
    """The git-side facts the SERVER-side freshen needs, resolved from the
    workspace's github binding: the ``repo`` (``owner/name``), its ``base_branch``,
    the decrypted push/fetch ``token`` (``None`` for a local ``file://`` remote in
    tests), and the ``remote_url`` used to RE-CLONE a reaped run workspace.

    Only this model has them. The founder's machine needs none of it — its
    checkout exists and its own credential does the push (§3.5).
    """

    repo: str
    base_branch: str
    token: str | None
    remote_url: str


async def freshen_in_clone(
    *, git: Any, clone: Path, branch: str, target: FreshnessTarget
) -> FreshenOutcome:
    """Merge ``origin/<base>`` into ``branch`` in a server-side clone, push if clean.

    The clone may have been reaped after the PR opened, so it is re-cloned at
    FULL depth when missing (a shallow one would lack the merge base).
    """
    try:
        if not (clone / ".git").exists():
            logger.info("merge_watch_reclone", clone=str(clone), branch=branch)
            clone.parent.mkdir(parents=True, exist_ok=True)
            # depth=0 → a FULL clone: a shallow re-clone would lack the merge base.
            await git.clone(target.remote_url, clone, token=target.token, depth=0)
            await git.checkout(clone, branch)
        await git.fetch(clone, "origin", target.base_branch, token=target.token, unshallow=True)
        result = await git.merge_ref(clone, f"origin/{target.base_branch}")
    except GitError:
        logger.warning("merge_watch_freshen_failed", clone=str(clone), exc_info=True)
        return FreshenOutcome(status="failed", base_branch=target.base_branch)

    if result.status != "clean":
        return FreshenOutcome(
            status="conflict",
            base_branch=target.base_branch,
            conflict_paths=tuple(result.conflict_paths),
        )

    try:
        await git.push(clone, branch, token=target.token)
    except GitError:
        logger.warning("merge_watch_freshen_failed", clone=str(clone), exc_info=True)
        return FreshenOutcome(status="failed", base_branch=target.base_branch)
    return FreshenOutcome(status="clean", base_branch=target.base_branch)


def build_server_freshener(
    *, cipher: Any, run_workspace_root: Path, git_ops: Any = None
) -> Callable[[AsyncSession, uuid.UUID, uuid.UUID, str], Awaitable[FreshenOutcome]]:
    """Freshen a watched PR in the run's SERVER-side clone.

    The clone may have been reaped after the PR opened, so it is re-cloned at
    FULL depth when missing (a shallow one would lack the merge base).
    """
    git: Any = git_ops or GitOps()

    async def _freshen(
        session: AsyncSession, workspace_id: uuid.UUID, run_id: uuid.UUID, branch: str
    ) -> FreshenOutcome:
        product_id = await product_id_for_run(session, run_id)
        binding = await resolve_github_binding(
            session, workspace_id=workspace_id, product_id=product_id
        )
        if binding is None:
            return FreshenOutcome(status="unavailable", base_branch="")
        creds = await resolve_connector_credentials(session, account=binding.account, cipher=cipher)
        # Persist any token refresh resolve performed under the hood.
        await session.commit()
        target = FreshnessTarget(
            repo=binding.repo,
            base_branch=binding.base_branch,
            token=creds["token"],
            remote_url=github_remote_url(binding.repo),
        )
        return await freshen_in_clone(
            git=git, clone=run_workspace_root / str(run_id), branch=branch, target=target
        )

    return _freshen


async def product_id_for_run(session: AsyncSession, run_id: uuid.UUID) -> uuid.UUID | None:
    """The product a run belongs to — the key to both its github binding and its
    execution model. A column-level scalar read (no ORM identity-map staleness)."""
    from sqlalchemy import select  # noqa: PLC0415

    from backend.workflow.infrastructure.db import ExecutionRun  # noqa: PLC0415

    return await session.scalar(select(ExecutionRun.product_id).where(ExecutionRun.id == run_id))


__all__ = [
    "FreshnessTarget",
    "build_server_freshener",
    "freshen_in_clone",
    "product_id_for_run",
]
