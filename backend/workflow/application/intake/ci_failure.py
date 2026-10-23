"""Send a PR's red CI back to the run that opened it.

The merge watch finds the checks on a run's PR failing. The PR is that run's work, so
the fix belongs to that run — same branch, same PR — exactly as for a merge conflict
(``build_merge_watch_conflict_redispatch``) or a review comment (``intake.pr_comment``).
Opening a new run would fork a second PR off main for every red check.

Two sides, like ``pr_comment``: :func:`send_ci_failure_to_run` writes
``run.payload["ci_failure"]`` and re-opens the run; the drive loop reads it once with
:func:`ci_failure_messages` and clears it with :func:`consume_ci_failure`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.workflow.application.run_status import move_run_status
from backend.workflow.infrastructure.db import ExecutionRun, RunStatus
from backend.workflow.infrastructure.github.db import GithubMergeWatchRow, MergeWatchStatus

logger = structlog.get_logger(__name__)

#: Runs that are finished from the loop's point of view but whose PR is not — they are
#: re-opened. An ``open`` / ``running`` run just gets the instruction queued.
_REOPEN = (RunStatus.REVIEW_READY, RunStatus.FAILED)
_QUEUE_ONLY = (RunStatus.OPEN, RunStatus.RUNNING)


async def send_ci_failure_to_run(
    session: AsyncSession,
    run: ExecutionRun,
    *,
    failed_checks: list[dict[str, str]],
    pr_number: int,
    head_sha: str | None,
) -> bool:
    """Queue the failed checks on ``run`` and re-open it; ``False`` if it can't take them.

    ``shipped`` is final and ``cancelled`` is the founder's own stop — neither is revived.
    The caller commits.
    """
    if run.status not in (*_REOPEN, *_QUEUE_ONLY):
        logger.info("ci_failure_not_sent_run_over", run_id=str(run.id), run_status=run.status.value)
        return False
    payload = dict(run.payload or {})
    payload["ci_failure"] = {
        "pr_number": pr_number,
        "head_sha": head_sha,
        "failed_checks": [dict(check) for check in failed_checks],
    }
    run.payload = payload
    if run.status in _REOPEN:
        names = ", ".join(c.get("name", "?") for c in failed_checks) or "checks"
        await move_run_status(
            session, run, RunStatus.OPEN, reason=f"CI failed on PR #{pr_number}: {names}"
        )
    await session.flush()
    return True


async def revive_merge_watch(
    session: AsyncSession, run: ExecutionRun, *, pr_number: int, ci_deadline_s: float
) -> None:
    """Put a ``failed`` watch of ``run``'s PR back to work after the founder sent it back.

    The fix is on its way, so the watch must see the new head and merge it: pending again,
    due now, a fresh CI deadline, and the hand-back budget (``ci_fix_*``) and the red-head
    memory cleared — this is a new attempt the founder asked for.
    """
    now = datetime.now(tz=UTC)
    await session.execute(
        update(GithubMergeWatchRow)
        .where(
            GithubMergeWatchRow.run_id == run.id,
            GithubMergeWatchRow.pr_number == pr_number,
            GithubMergeWatchRow.status == MergeWatchStatus.FAILED,
        )
        .values(
            status=MergeWatchStatus.PENDING_CI,
            next_poll_at=now,
            deadline_at=now + timedelta(seconds=ci_deadline_s),
            last_error="ci_fix_requested_by_founder",
            ci_fix_attempts=0,
            ci_fix_head_sha=None,
            ci_red_head_sha=None,
        )
    )


def ci_failure_messages(run: ExecutionRun) -> list[dict[str, Any]]:
    """The failed checks of this run's PR, as turn-context messages (empty if none)."""
    payload = run.payload if isinstance(run.payload, dict) else {}
    failure = payload.get("ci_failure")
    if not isinstance(failure, dict):
        return []
    lines = []
    for check in failure.get("failed_checks") or []:
        if not isinstance(check, dict):
            continue
        line = f"- {check.get('name') or 'unnamed check'} ({check.get('conclusion') or 'failed'})"
        if check.get("summary"):
            line += f"\n{check['summary']}"
        lines.append(line)
    detail = "\n".join(lines) or "- (the checks did not say which one)"
    return [
        {
            "role": "user",
            "content": (
                f"The CI checks on your pull request #{failure.get('pr_number')} failed:\n\n"
                f"{detail}\n\n"
                "Reproduce and fix the failure on the SAME branch so the result is pushed to "
                "the same pull request. Do not open a new pull request."
            ),
        }
    ]


def consume_ci_failure(run: ExecutionRun) -> None:
    """Clear the one-shot ``ci_failure`` key once injected (re-assigns the payload so
    SQLAlchemy sees the JSON change)."""
    payload = dict(run.payload or {})
    if "ci_failure" not in payload:
        return
    payload.pop("ci_failure", None)
    run.payload = payload


__all__ = [
    "ci_failure_messages",
    "consume_ci_failure",
    "revive_merge_watch",
    "send_ci_failure_to_run",
]
