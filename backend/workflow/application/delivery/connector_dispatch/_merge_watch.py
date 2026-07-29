"""CI-green auto-merge enqueue (PR4).

Kept in its own sibling module (out of ``_github.py``) so the github delivery
handler stays under the Lift §17.7 ≤400 LOC cap. One function:
:func:`enqueue_merge_watch` — register a just-opened PR under CI-green auto-merge
watch (a ``github_merge_watch`` row) when ``github_auto_merge_enabled`` is on.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import structlog

from backend.workflow.infrastructure.github.db import GithubMergeWatchRow, MergeWatchStatus
from backend.workflow.infrastructure.github.repository import GithubMergeWatchRepository

if TYPE_CHECKING:
    from ._github import GithubDeliveryDeps
    from ._resolver import GithubBinding

logger = structlog.get_logger(__name__)


async def enqueue_merge_watch(
    deps: GithubDeliveryDeps,
    *,
    binding: GithubBinding,
    workspace_id: uuid.UUID,
    run_id: uuid.UUID,
    deliverable_id: uuid.UUID,
    branch: str,
    pr_number: int,
) -> None:
    """PR4 — register the opened PR under CI-green auto-merge watch.

    No-op when ``deps.settings`` is absent or ``github_auto_merge_enabled`` is
    off — so the flag-off path inserts NOTHING and the existing github-delivery
    behavior is byte-identical. Soft — an enqueue hiccup is logged, never raised
    into the delivery path (the PR is already open; a missed watch row just means
    the founder merges it by hand, the pre-PR4 behavior)."""
    settings = deps.settings
    if settings is None or not settings.github_auto_merge_enabled:
        return
    now = datetime.now(tz=UTC)
    row = GithubMergeWatchRow(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        run_id=run_id,
        deliverable_id=deliverable_id,
        repo=binding.repo,
        pr_number=pr_number,
        branch=branch,
        base_branch=binding.base_branch,
        status=MergeWatchStatus.PENDING_CI,
        attempts=0,
        next_poll_at=now,
        deadline_at=now + timedelta(seconds=settings.github_auto_merge_ci_deadline_s),
        conflict_dispatched=False,
        created_at=now,
    )
    try:
        async with deps.session_factory() as session:
            await GithubMergeWatchRepository(session).add(row)
            await session.commit()
        logger.info(
            "github_merge_watch_enqueued",
            workspace_id=str(workspace_id),
            deliverable_id=str(deliverable_id),
            repo=binding.repo,
            pr_number=pr_number,
            branch=branch,
        )
    except Exception:  # noqa: BLE001 — enqueue must never fail an already-open PR
        logger.warning(
            "github_merge_watch_enqueue_failed",
            workspace_id=str(workspace_id),
            deliverable_id=str(deliverable_id),
            repo=binding.repo,
            pr_number=pr_number,
            exc_info=True,
        )


__all__ = ["enqueue_merge_watch"]
