"""Merge-watch hand-backs to the original run: merge conflict and red CI.

Split out of ``merge_watch_runtime`` to keep that module under the runtime LOC cap. Both
callbacks write their context onto the run that opened the PR and re-open it, so the
agent fixes the SAME branch; they are injected into the infrastructure worker as opaque
callables.
"""

from __future__ import annotations

import uuid

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.workflow.infrastructure.db import ExecutionRun, RunStatus
from backend.workflow.infrastructure.workers.merge_watch_worker import (
    CiRedispatch,
    ConflictRedispatch,
)

logger = structlog.get_logger(__name__)


def build_merge_watch_conflict_redispatch(
    *, session_factory: async_sessionmaker[AsyncSession]
) -> ConflictRedispatch:
    """Build the PR6 conflict re-dispatch callback for the MergeWatchWorker.

    Writing ``run.payload["merge_conflict"]`` + transitioning the run RUNNING →
    OPEN (so ``AgentWorker.drive_once`` re-picks it and the agent resolves the
    conflict — PR7's side) are APPLICATION concerns, so they live here and are
    injected into the infrastructure worker as an opaque callable. Uses the SAME
    RUNNING → OPEN resume seam ``checkpoint_resolution.resolve_checkpoint`` uses.
    Opens its own short transaction (the worker's session holds the per-repo lock)
    and is idempotent — a no-op run id is skipped; a re-open of an already-OPEN
    run no-ops in :meth:`AgentRunner.transition`.
    """

    async def _redispatch(
        run_id: uuid.UUID,
        *,
        conflict_paths: list[str],
        base_branch: str,
        pr_number: int,
    ) -> None:
        from backend.workflow.application.agent_runner import AgentRunner  # noqa: PLC0415

        async with session_factory() as session:
            run = await session.get(ExecutionRun, run_id)
            if run is None:
                logger.warning("merge_watch_redispatch_run_missing", run_id=str(run_id))
                return
            # Re-assign payload (not in-place mutate) so SQLAlchemy detects the
            # change on the JSON column — mirrors checkpoint_resolution.
            payload = dict(run.payload or {})
            payload["merge_conflict"] = {
                "conflict_paths": list(conflict_paths),
                "base_branch": base_branch,
                "pr_number": pr_number,
            }
            # Conflict-robustness — EVERY re-dispatch delivers the conflict
            # context to the agent afresh: writing ``merge_conflict`` restores the
            # one-shot directive (the drive loop re-injects + re-consumes it), and
            # clearing a stale ``merge_conflict_resolving`` marker (left by a
            # PRIOR turn that consumed the directive but then failed) keeps the
            # retried re-drive clean — the drive loop re-sets the marker itself.
            payload.pop("merge_conflict_resolving", None)
            run.payload = payload
            runner = AgentRunner(session)
            await runner.transition(
                run_id=run_id,
                to_status=RunStatus.OPEN,
                reason=f"merge conflict on PR #{pr_number}: freshen against {base_branch}",
            )
            await session.commit()
        logger.info(
            "merge_watch_conflict_redispatched",
            run_id=str(run_id),
            pr_number=pr_number,
            conflict_paths=conflict_paths,
        )

    return _redispatch


def build_merge_watch_ci_redispatch(
    *, session_factory: async_sessionmaker[AsyncSession]
) -> CiRedispatch:
    """Build the red-CI hand-back for the MergeWatchWorker (the conflict redispatch's twin).

    Writes the failed checks on the run that opened the PR and re-opens it, so the agent
    fixes the SAME branch. Opens its own short transaction; a missing or finished run is a
    no-op (the worker then simply watches on, and asks the founder when its patience ends).
    """

    async def _redispatch(
        run_id: uuid.UUID,
        *,
        failed_checks: list[dict[str, str]],
        pr_number: int,
        head_sha: str,
    ) -> None:
        from backend.workflow.application.intake.ci_failure import (  # noqa: PLC0415
            send_ci_failure_to_run,
        )

        async with session_factory() as session:
            run = await session.get(ExecutionRun, run_id)
            if run is None:
                logger.warning("merge_watch_ci_redispatch_run_missing", run_id=str(run_id))
                return
            await send_ci_failure_to_run(
                session, run, failed_checks=failed_checks, pr_number=pr_number, head_sha=head_sha
            )
            await session.commit()
        logger.info(
            "merge_watch_ci_redispatched",
            run_id=str(run_id),
            pr_number=pr_number,
            failed_checks=[c.get("name") for c in failed_checks],
        )

    return _redispatch


__all__ = [
    "build_merge_watch_ci_redispatch",
    "build_merge_watch_conflict_redispatch",
]
