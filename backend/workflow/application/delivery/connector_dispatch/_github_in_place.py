"""Delivery for a run that executed on the founder's own machine.

Everything the server-side path does with git is already done, elsewhere and by
someone else: the run committed in its own worktree (#734) and pushed with the
FOUNDER's git credential (#735). No token travelled from here, and no source
travels to here (§3.5 privacy contract).

So what is left is the half that is purely an API call about a branch that
exists — which is exactly what #723 removed when it turned the whole github
binding off for this model. The crash it fixed was real; the conclusion was too
wide.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.connectors.auth.resolve import resolve_connector_credentials
from backend.workflow.domain.client_worktree import worktree_branch
from backend.workflow.domain.delivery import ActionResult
from backend.workflow.infrastructure.db import ExecutionRun

from ._builders import _split_summary
from ._context import _build_context
from ._github_pr import _open_pr_and_settle, _source_github_issue_number
from ._resolver import GithubBinding, product_runs_in_place

if TYPE_CHECKING:
    from ._github import GithubDeliveryDeps

logger = structlog.get_logger(__name__)


async def _run_is_client_attach(
    session_factory: async_sessionmaker[AsyncSession], run_id: uuid.UUID
) -> bool:
    """Does this run execute on the founder's machine rather than the server?

    Read here rather than threaded in: WHERE a run executed is a fact about the
    run, and delivery happens long after (on approval, in another process).
    """
    from sqlalchemy import select  # noqa: PLC0415

    try:
        async with session_factory() as session:
            product_id = await session.scalar(
                select(ExecutionRun.product_id).where(ExecutionRun.id == run_id)
            )
            if product_id is None:
                return False
            return await product_runs_in_place(session, product_id)
    except Exception:  # noqa: BLE001 — an unreadable run keeps the default model
        logger.warning("github_delivery_execution_model_lookup_failed", run_id=str(run_id))
        return False


async def _deliver_client_attach_pr(
    *,
    deps: GithubDeliveryDeps,
    binding: GithubBinding,
    workspace_id: uuid.UUID,
    deliverable_id: uuid.UUID,
    run_id: uuid.UUID,
    content: dict[str, Any],
) -> list[ActionResult]:
    """Open a PR for a run that committed and pushed on the founder's machine.

    Everything the server-side path does with git is already done, elsewhere and
    by someone else: since #735 the run commits in its own worktree (#734) and
    pushes with the FOUNDER's git credential — no token travels from here, and
    no source travels to here (§3.5). What is left is the half that is purely an
    API call about a branch that exists.

    **No empty PR**, the same rule the server-side path holds — but asked of
    github rather than of a local checkout, because there is no local checkout
    and because a local record can disagree with what actually landed (a push
    that failed after the commit; an earlier attempt's branch already there).
    The remote is the only authority on whether a PR can be opened at all.
    """
    action_prefix = "github:outbound:pr"
    branch = worktree_branch(run_id)
    summary = str(content.get("summary") or "")
    title, body = _split_summary(summary)

    plugin = deps.plugins_by_name.get("github")
    if plugin is None:
        return [
            ActionResult(action=action_prefix, succeeded=False, error="github plugin not loaded")
        ]

    async with deps.session_factory() as session:
        creds = await resolve_connector_credentials(
            session, account=binding.account, cipher=deps.cipher
        )
        # Persist any token refresh resolve performed under the hood.
        await session.commit()
        source_issue = await _source_github_issue_number(session, run_id)
    token = creds["token"]

    ctx = _build_context(credentials={"token": token}, config=dict(binding.account.delivery_config))
    try:
        compared = await deps.runner.dispatch_action(
            plugin,
            action_name="compare_branch",
            context=ctx,
            kwargs={"repo": binding.repo, "head": branch, "base": binding.base_branch},
        )
    except Exception as exc:  # noqa: BLE001 — soft-fail like a plugin failure
        logger.warning(
            "github_delivery_compare_failed",
            workspace_id=str(workspace_id),
            run_id=str(run_id),
            branch=branch,
            error=str(exc),
        )
        return [ActionResult(action=action_prefix, succeeded=False, error=str(exc))]

    compared = compared if isinstance(compared, dict) else {}
    if not compared.get("exists") or int(compared.get("ahead_by") or 0) <= 0:
        # Nothing landed on this branch: the run changed nothing, or its push
        # never made it. Either way an empty PR would be a claim that work
        # happened — and it is not a delivery FAILURE, exactly as on the server
        # side. The run's own record of the push is in its ``LoopTerminal``
        # audit (#735) for anyone asking which of the two it was.
        logger.info(
            "github_delivery_no_changes_noop",
            workspace_id=str(workspace_id),
            deliverable_id=str(deliverable_id),
            run_id=str(run_id),
            branch=branch,
            mode="client_attach",
        )
        return [
            ActionResult(
                action=action_prefix,
                succeeded=True,
                output={"skipped": True, "reason": "no_changes"},
            )
        ]

    if source_issue is not None:
        body = f"{body}\n\nCloses #{source_issue}".strip()

    return await _open_pr_and_settle(
        deps=deps,
        binding=binding,
        workspace_id=workspace_id,
        deliverable_id=deliverable_id,
        run_id=run_id,
        branch=branch,
        title=title,
        body=body,
        token=token,
        source_issue=source_issue,
        action_prefix=action_prefix,
        pushed_by="founder_machine",
    )


__all__ = ["_deliver_client_attach_pr", "_run_is_client_attach"]
