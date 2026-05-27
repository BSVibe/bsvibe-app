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
    Decision,
    DecisionStatus,
    ExecutionRun,
    ExecutionRunHistory,
    RunStatus,
)
from backend.execution.orchestrator import LoopResult, RunCompute
from backend.intake.db import RequestRow

#: L-P2: Decision kind synthesized when a run enters REVIEW_READY (verified
#: + deliverable created, awaiting founder's ship/discard call). Reuses the
#: shared one-click action map from :mod:`backend.api.v1.checkpoints` so the
#: PWA renders the same buttons as for executor B2b Decisions.
SHIP_OR_DISCARD_DECISION_KIND = "ship_or_discard"

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

        L-P2: when transitioning to ``REVIEW_READY``, also synthesize a
        ``ship_or_discard`` Decision so the run surfaces in the founder's
        Decisions UI with one-click ship/discard buttons. Without this,
        verified-but-unshipped runs sat invisible — the e2e-hello reality
        audit hit exactly this case.
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
        if to_status is RunStatus.REVIEW_READY:
            await self._mint_ship_or_discard_decision(run)
        await self._session.flush()
        logger.info(
            "agent_runner_transitioned",
            run_id=str(run_id),
            from_status=from_status.value,
            to_status=to_status.value,
        )
        return True

    async def _mint_ship_or_discard_decision(self, run: ExecutionRun) -> None:
        """Synthesize a pending ``ship_or_discard`` Decision for ``run``.

        Idempotent: a second transition into REVIEW_READY (or a retry) won't
        mint duplicates; an already-pending Decision of this kind on this
        run short-circuits. The Decision payload carries no question — the
        founder-facing line comes from
        :data:`backend.api.v1.checkpoints._EXECUTOR_DECISION_QUESTIONS`'s
        kind-derived fallback.
        """
        from sqlalchemy import select  # noqa: PLC0415 — local-only

        existing = (
            await self._session.execute(
                select(Decision).where(
                    Decision.run_id == run.id,
                    Decision.decision == SHIP_OR_DISCARD_DECISION_KIND,
                    Decision.status == DecisionStatus.PENDING,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return
        self._session.add(
            Decision(
                id=uuid.uuid4(),
                run_id=run.id,
                workspace_id=run.workspace_id,
                decision=SHIP_OR_DISCARD_DECISION_KIND,
                payload={"reason": "review_ready"},
                status=DecisionStatus.PENDING,
            )
        )

    async def _find_existing_run(self, *, request_id: uuid.UUID) -> ExecutionRun | None:
        from sqlalchemy import select  # noqa: PLC0415 — local-only lookup

        stmt = select(ExecutionRun).where(ExecutionRun.request_id == request_id).limit(1)
        return (await self._session.execute(stmt)).scalar_one_or_none()


__all__ = ["AgentRunner"]
