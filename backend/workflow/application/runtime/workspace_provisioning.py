"""W1 workspace provisioning — put the run's source on disk before work starts.

Two provisioners and the composition that orders them:

* **github** (injected) — clone/fetch the run's GitHub repo.
* **product** — restore the product repo from its durable R2 bundle and add a
  per-run ``git worktree``.

Split out of ``agent_runtime`` (audit §17.2): that module holds the agent
execution-deps FACTORY, and provisioning is a separate concern with its own
tests. Both keep their leading underscore — they are re-exported through
``workflow.infrastructure.workers.run``, which is the seam callers and tests use.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from backend.workflow.application.runtime.account_resolution import product_is_client_attach
from backend.workflow.infrastructure.db import ExecutionRun

logger = structlog.get_logger(__name__)

__all__ = [
    "_build_composite_workspace_provisioner",
    "_product_workspace_provisioner",
]


async def _product_workspace_provisioner(
    session: AsyncSession,
    run: ExecutionRun,
    workspace_dir: Path,
) -> bool:
    """W1: provision the run's workspace_dir as a git worktree of the product's
    main branch, restoring the product repo from its durable bundle if it is not
    on disk."""
    if run.product_id is None:
        return False

    from backend.storage.product_workspace import (  # noqa: PLC0415 — lazy
        add_run_worktree,
        ensure_or_init_product_workspace,
    )

    await ensure_or_init_product_workspace(run.product_id)
    if workspace_dir.exists() and not any(workspace_dir.iterdir()):  # noqa: ASYNC240
        workspace_dir.rmdir()  # noqa: ASYNC240
    await add_run_worktree(run.product_id, run.id)
    return True


def _build_composite_workspace_provisioner(
    *,
    github: Callable[[AsyncSession, ExecutionRun, Path], Awaitable[None]],
    product: Callable[[AsyncSession, ExecutionRun, Path], Awaitable[bool]],
) -> Callable[[AsyncSession, ExecutionRun, Path], Awaitable[None]]:
    """Compose the two W1 provisioners in priority order. #692 — SKIPPED for a
    ``client_attach`` product: both put its source into a server-side worktree,
    and local execution means it stays on the user's machine."""

    async def _composed(session: AsyncSession, run: ExecutionRun, workspace_dir: Path) -> None:
        if run.product_id is not None and await product_is_client_attach(session, run.product_id):
            logger.info("client_attach_server_workspace_skipped", run_id=str(run.id))
            return
        await github(session, run, workspace_dir)
        if not workspace_dir.exists() or any(workspace_dir.iterdir()):  # noqa: ASYNC240
            return
        await product(session, run, workspace_dir)

    return _composed
