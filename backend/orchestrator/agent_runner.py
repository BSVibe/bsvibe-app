"""AgentRunner — drive the execution layer for one Request.

Workflow §12.5 #8 (Bundle G — Orchestrator). The agent runner is the
bridge between the workflow state machine and the execution layer
(Bundle X). It opens an ExecutionRun row, advances it through the
``open → running → review_ready → shipped`` lifecycle, and surfaces
the resulting run_id.

It opens the run, then delegates the compute loop to
``backend.execution.orchestrator.RunOrchestrator`` and maps the loop's
terminal outcome back onto the run status:

* ``verified`` → ``review_ready`` (work done, awaiting ship/delivery).
* ``needs_decision`` → run stays ``running`` (paused on a Decision row;
  resolution re-enters the loop — not a DB terminal).
* ``system_error`` → ``failed``.

This module owns the *transactional* lifecycle; ``RunOrchestrator`` owns
the *compute* lifecycle.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from backend.execution.db import (
    ExecutionRun,
    ExecutionRunHistory,
    RunStatus,
)
from backend.execution.orchestrator import LoopResult, RunCompute
from backend.intake.db import RequestRow

logger = structlog.get_logger(__name__)


class AgentRunner:
    """Spawn + supervise one ExecutionRun for a Request."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def open_run(self, *, request: RequestRow) -> uuid.UUID:
        """Mint an ExecutionRun row tied to ``request``; returns run_id.

        Idempotent: if a run for this request already exists, returns its
        id without creating a duplicate.
        """
        existing = await self._find_existing_run(request_id=request.id)
        if existing is not None:
            return existing.id

        run = ExecutionRun(
            id=uuid.uuid4(),
            workspace_id=request.workspace_id,
            # L-P1: propagate product_id from the Request (the Request copies
            # it from the TriggerEvent during intake). The previous hardcoded
            # ``None`` is what dropped product binding on every run, so e.g.
            # founder-direct submits never showed up on a product detail page.
            product_id=request.product_id,
            request_id=request.id,
            status=RunStatus.OPEN,
            payload={"request_id": str(request.id)},
            created_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC),
        )
        self._session.add(run)
        self._session.add(
            ExecutionRunHistory(
                id=uuid.uuid4(),
                run_id=run.id,
                workspace_id=request.workspace_id,
                from_status=None,
                to_status=RunStatus.OPEN,
                reason="opened by agent_runner",
                created_at=datetime.now(tz=UTC),
            )
        )
        await self._session.flush()
        logger.info(
            "agent_runner_opened",
            request_id=str(request.id),
            run_id=str(run.id),
        )
        return run.id

    async def drive(
        self,
        *,
        run_id: uuid.UUID,
        orchestrator: RunCompute,
        workspace_dir: Path,
    ) -> LoopResult:
        """Run the compute loop for ``run_id`` and reconcile its outcome
        with the transactional run status.

        ``orchestrator`` is any :class:`RunCompute` — the native
        :class:`~backend.execution.orchestrator.RunOrchestrator` (api-llm) or
        the :class:`~backend.executors.orchestrator.ExecutorOrchestrator`
        (CLI-worker dispatch). Both have the same ``run(...) -> LoopResult``
        shape, so the outcome mapping below is backend-agnostic.

        Transitions ``open → running`` before the loop, then maps the
        terminal outcome: ``verified → review_ready``, ``system_error →
        failed``, ``needs_decision`` leaves the run ``running`` (paused).
        """
        run = await self._session.get(ExecutionRun, run_id)
        if run is None:
            raise ValueError(f"ExecutionRun {run_id} not found")

        await self.transition(
            run_id=run_id, to_status=RunStatus.RUNNING, reason="agent loop started"
        )
        result = await orchestrator.run(run=run, workspace_dir=workspace_dir)

        if result.outcome == "verified":
            await self.transition(
                run_id=run_id, to_status=RunStatus.REVIEW_READY, reason="agent loop verified"
            )
        elif result.outcome == "system_error":
            await self.transition(
                run_id=run_id,
                to_status=RunStatus.FAILED,
                reason=result.summary or "agent loop system error",
            )
        # needs_decision: run stays RUNNING (paused on a Decision row).
        logger.info(
            "agent_runner_loop_complete",
            run_id=str(run_id),
            outcome=result.outcome,
        )
        return result

    async def transition(
        self,
        *,
        run_id: uuid.UUID,
        to_status: RunStatus,
        reason: str | None = None,
    ) -> bool:
        """Append history + flip ExecutionRun.status. Returns False on no-op.

        W2: when transitioning to ``REVIEW_READY`` on a product-bound run,
        auto-ship — fast-forward main onto the run branch (under advisory
        lock) and cascade to SHIPPED. Non-product runs (Direct-path / no
        product binding) transition to REVIEW_READY and stay there
        unchanged, matching pre-W1 behavior for tests + legacy code paths.
        """
        run = await self._session.get(ExecutionRun, run_id)
        if run is None:
            return False
        if run.status is to_status:
            return False
        from_status = run.status
        run.status = to_status
        run.updated_at = datetime.now(tz=UTC)
        self._session.add(
            ExecutionRunHistory(
                id=uuid.uuid4(),
                run_id=run_id,
                workspace_id=run.workspace_id,
                from_status=from_status,
                to_status=to_status,
                reason=reason,
                created_at=datetime.now(tz=UTC),
            )
        )
        await self._session.flush()
        logger.info(
            "agent_runner_transitioned",
            run_id=str(run_id),
            from_status=from_status.value,
            to_status=to_status.value,
        )

        # W2 — auto-ship on REVIEW_READY for product-bound runs that
        # actually have a git worktree on disk. Glue tests that bypass
        # the workspace provisioner (no worktree) skip auto-ship and
        # leave the run at REVIEW_READY — exactly the pre-W2 invariant.
        if to_status is RunStatus.REVIEW_READY and run.product_id is not None:
            from backend.storage.product_workspace import (  # noqa: PLC0415
                run_worktree_path,
            )

            if (run_worktree_path(run.id) / ".git").exists():
                await self._auto_ship_product_run(run)
        return True

    async def _auto_ship_product_run(self, run: ExecutionRun) -> None:
        """Fast-forward main onto the run branch and transition to SHIPPED.

        Pre-conditions: ``verify`` already ran ``commit_worktree`` +
        ``merge_main_into_worktree`` (cleanly) before transitioning to
        REVIEW_READY, so the run's branch is a strict descendant of main.
        The advisory lock here protects the ``merge_to_main``
        fast-forward from a parallel ship moving main between the
        verify-time merge and this call.

        Failure modes:

        * Lock busy → leave run at REVIEW_READY; next AgentWorker tick
          (or a follow-up trigger) retries. Doesn't block.
        * Fast-forward refused (rare — verify-time merge stale) → leave
          run at REVIEW_READY with a history note. The next verify
          round will pull main again.
        * Worktree cleanup fails → logged, run still ships (cleanup is
          best-effort; the next worker tick retries).
        """
        from backend.storage.product_workspace import (  # noqa: PLC0415 — lazy
            ProductWorkspaceBusy,
            ProductWorkspaceError,
            merge_to_main,
            product_workspace_lock,
            remove_run_worktree,
        )

        product_id = run.product_id
        if product_id is None:
            return

        try:
            async with product_workspace_lock(self._session, product_id):
                sha = await merge_to_main(product_id, run.id)
                logger.info(
                    "auto_ship_merge_to_main",
                    run_id=str(run.id),
                    product_id=str(product_id),
                    main_sha=sha,
                )
            # Transition past REVIEW_READY → SHIPPED. The history row
            # is recorded directly here rather than re-calling
            # ``transition`` (which would recurse).
            run.status = RunStatus.SHIPPED
            run.updated_at = datetime.now(tz=UTC)
            self._session.add(
                ExecutionRunHistory(
                    id=uuid.uuid4(),
                    run_id=run.id,
                    workspace_id=run.workspace_id,
                    from_status=RunStatus.REVIEW_READY,
                    to_status=RunStatus.SHIPPED,
                    reason="auto-shipped after verify",
                    created_at=datetime.now(tz=UTC),
                )
            )
            await self._session.flush()
            # Best-effort worktree cleanup (idempotent — covers retries).
            try:
                await remove_run_worktree(product_id, run.id)
            except ProductWorkspaceError:
                logger.warning(
                    "auto_ship_worktree_cleanup_failed",
                    run_id=str(run.id),
                    exc_info=True,
                )
        except ProductWorkspaceBusy:
            logger.info(
                "auto_ship_lock_busy",
                run_id=str(run.id),
                product_id=str(product_id),
            )
            # Leave at REVIEW_READY; next tick retries.
        except ProductWorkspaceError:
            logger.warning(
                "auto_ship_merge_failed",
                run_id=str(run.id),
                product_id=str(product_id),
                exc_info=True,
            )
            # Leave at REVIEW_READY; next verify round will pull main
            # again and either succeed or surface a conflict.

    async def _find_existing_run(self, *, request_id: uuid.UUID) -> ExecutionRun | None:
        from sqlalchemy import select  # noqa: PLC0415 — local-only lookup

        stmt = select(ExecutionRun).where(ExecutionRun.request_id == request_id).limit(1)
        return (await self._session.execute(stmt)).scalar_one_or_none()


__all__ = ["AgentRunner"]
