"""Reaching the founder's machine from the merge-watch poller.

The agent loop builds this same box when it starts a client_attach run (#703,
via :mod:`...runtime.sandbox_selection`). The poller needs one much later, in
another process, with no run in flight — so it resolves the same facts from the
same places: the product says WHERE (``client_workspace_path``) and the
workspace model account says HOW to get there (``executor_type`` + the pinned
worker that ran this product's turns).

``None`` at any step means that machine cannot be reached, which the caller must
treat as a failure and never as permission to do the work here instead.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.workflow.infrastructure.db import ExecutionRun

logger = structlog.get_logger(__name__)

#: A git round trip to that machine, not a build.
_BOX_TIMEOUT_S = 300.0


async def resolve_base_branch(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    product_id: uuid.UUID | None,
    cipher: Any,
) -> str | None:
    """The base branch this run's PR targets, from its product github binding.

    Only the BRANCH NAME is read — no token is decrypted and nothing is sent to
    the founder's machine. Their own git credential does the fetch and the push
    (§3.5), exactly as #735's commit does.
    """
    from backend.workflow.application.delivery.connector_dispatch._resolver import (  # noqa: PLC0415
        resolve_github_binding,
    )

    binding = await resolve_github_binding(
        session, workspace_id=workspace_id, product_id=product_id
    )
    return binding.base_branch if binding is not None else None


def client_box_factory(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    redis_client: Any,
    workspace_id: uuid.UUID,
    product_id: uuid.UUID | None,
) -> Callable[[uuid.UUID], Awaitable[Any]]:
    """Build a way to get a box rooted in one run's worktree on that machine.

    Acquiring the box PROVISIONS the run's worktree if it is gone — #736 hands
    it back when the run finishes, and a PR goes stale long after that. The
    provisioning command is idempotent and already handles "the branch exists
    but its directory was removed" (#734), which is exactly this case.
    """

    async def _box(run_id: uuid.UUID) -> Any:
        from backend.workflow.application.runtime.account_resolution import (  # noqa: PLC0415
            product_dispatch_config,
            resolve_workspace_model_account,
        )
        from backend.workflow.application.runtime.sandbox_selection import (  # noqa: PLC0415
            sandbox_manager_for_run,
        )
        from backend.workflow.infrastructure.sandbox import NoopSandboxManager  # noqa: PLC0415

        if product_id is None or redis_client is None:
            return None
        try:
            async with session_factory() as session:
                _repo, target, client_dir = await product_dispatch_config(session, product_id)
                # The SAME resolution the run's own turns went through, so the box
                # lands on the machine that actually holds this product's checkout
                # — the account carries the pinned ``worker_id``.
                run = await session.get(ExecutionRun, run_id)
                if run is None:
                    return None
                account = await resolve_workspace_model_account(session, run)
            if account is None or not client_dir:
                logger.warning(
                    "merge_watch_client_box_context_incomplete",
                    run_id=str(run_id),
                    has_account=account is not None,
                    has_client_dir=bool(client_dir),
                )
                return None
            # A sentinel default: if the selection declines (an incomplete
            # dispatch context), it hands back what it was given — and a server
            # box here would run the freshen in the wrong place entirely. So the
            # default is one that cannot be mistaken for the founder's machine,
            # and receiving it back means "unreachable".
            sentinel = NoopSandboxManager()
            manager = sandbox_manager_for_run(
                default=sentinel,
                execution_target=target,
                client_workspace_dir=client_dir,
                account=account,
                redis_client=redis_client,
                session_factory=session_factory,
                workspace_id=workspace_id,
                timeout_s=_BOX_TIMEOUT_S,
                run_id=run_id,
            )
            if manager is sentinel:
                return None
            return await manager.acquire(run_id, client_dir)
        except Exception:  # noqa: BLE001 — an unreachable machine is a soft failure
            logger.warning("merge_watch_client_box_failed", run_id=str(run_id), exc_info=True)
            return None

    return _box


__all__ = ["client_box_factory", "resolve_base_branch"]
