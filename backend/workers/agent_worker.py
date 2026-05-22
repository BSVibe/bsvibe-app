"""AgentWorker — consume pending Requests and advance them via AgentRunner.

Workflow §12.5 #8 (Bundle G — Workers). DB-polling implementation (not
Redis Streams) — pulls ``status=OPEN`` Requests from the ``requests``
table, claims them via row-update, and hands each to
:class:`backend.orchestrator.AgentRunner` to mint an ExecutionRun.

The worker advances a Request through two single-tick phases so each can
be driven deterministically in a test:

* :meth:`claim_once` — claim ``OPEN`` Requests → ``open_run`` an
  ExecutionRun (status ``OPEN``) + flip the Request to ``RUNNING``.
* :meth:`drive_once` — for each ExecutionRun still ``OPEN``, *frame* the
  Request (:class:`~backend.orchestrator.frame.FrameStage`) then *drive*
  the agent loop (:class:`~backend.orchestrator.agent_runner.AgentRunner`
  delegating compute to :class:`~backend.execution.orchestrator.RunOrchestrator`),
  mapping ``verified → review_ready`` etc.

``drive_once`` needs an execution backend (a work-LLM seam + a sandbox +
the workspace skill registry). That backend is injected as the optional
:class:`AgentExecutionDeps`; without it the worker only *stages* runs
(claim) — the behaviour relied on by the narrow lifecycle tests and used
before an execution backend is provisioned. The production ``_tick`` runs
both phases.

The Redis Streams variant (with proper consumer-group semantics + XACK)
remains a TODO — for Phase 1 the DB-polling path is simpler to reason
about and integration-test, and the load is bounded by Request volume.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.execution.db import ExecutionRun, RunStatus
from backend.execution.orchestrator import RunOrchestrator
from backend.intake.db import RequestRow, RequestStatus
from backend.orchestrator.agent_runner import AgentRunner
from backend.orchestrator.frame import FrameConfig, FrameStage
from backend.skills.loader import SkillLoader
from backend.workers.base import BaseWorker

logger = structlog.get_logger(__name__)


@dataclass(slots=True)
class AgentWorkerConfig:
    batch_size: int = 10
    poll_interval_s: float = 5.0


@dataclass(slots=True)
class AgentExecutionDeps:
    """The execution backend :meth:`AgentWorker.drive_once` needs.

    * ``skill_loader`` — frames the Request against the workspace skills.
    * ``orchestrator_factory`` — builds a :class:`RunOrchestrator` bound to
      the *same* session the run is driven in (so compute + transactional
      lifecycle share one transaction). Production injects the gateway
      work-LLM + real sandbox; tests inject the scripted LLM + Noop sandbox.
    * ``workspace_root`` — each run drives inside ``workspace_root/<run_id>``.
    * ``default_artifact_type`` — frame hint when no skill matches.
    """

    skill_loader: SkillLoader
    orchestrator_factory: Callable[[AsyncSession], RunOrchestrator]
    workspace_root: Path
    default_artifact_type: str | None = "direct_output"


class AgentWorker(BaseWorker):
    """DB-polling worker that claims Requests and drives them through the loop."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        config: AgentWorkerConfig | None = None,
        execution: AgentExecutionDeps | None = None,
    ) -> None:
        self._cfg = config or AgentWorkerConfig()
        super().__init__(name="agent_worker", poll_interval_s=self._cfg.poll_interval_s)
        self._session_factory = session_factory
        self._execution = execution
        self._frame_stage = FrameStage()

    async def _tick(self) -> int:
        claimed = await self.claim_once()
        driven = await self.drive_once()
        return claimed + driven

    async def claim_once(self) -> int:
        """Pull one batch of OPEN Requests, open a run + flip to RUNNING. Returns count."""
        count = 0
        async with self._session_factory() as session:
            async for req in self._claim_batch(session):
                runner = AgentRunner(session)
                run_id = await runner.open_run(request=req)
                req.status = RequestStatus.RUNNING
                await session.flush()
                logger.info(
                    "agent_worker_claimed",
                    request_id=str(req.id),
                    run_id=str(run_id),
                )
                count += 1
            await session.commit()
        return count

    async def drive_once(self) -> int:
        """Frame + drive each ExecutionRun still OPEN. Returns count driven.

        No-op (returns 0) when no :class:`AgentExecutionDeps` were injected —
        the worker can only stage runs without an execution backend.
        """
        execution = self._execution
        if execution is None:
            return 0
        count = 0
        async with self._session_factory() as session:
            stmt = (
                select(ExecutionRun)
                .where(ExecutionRun.status == RunStatus.OPEN)
                .order_by(ExecutionRun.created_at.asc())
                .limit(self._cfg.batch_size)
                .with_for_update(skip_locked=True)
            )
            runs = (await session.execute(stmt)).scalars().all()
            for run in runs:
                await self._frame_and_drive(session, run, execution)
                count += 1
            await session.commit()
        return count

    async def _frame_and_drive(
        self, session: AsyncSession, run: ExecutionRun, execution: AgentExecutionDeps
    ) -> None:
        """Frame the run's Request, fold the hints + intent text into the run
        payload, then drive the agent loop to a terminal outcome."""
        if run.request_id is not None:
            request = await session.get(RequestRow, run.request_id)
            if request is not None:
                framed = await self._frame_stage.frame(
                    request=request,
                    config=FrameConfig(
                        skill_loader=execution.skill_loader,
                        default_artifact_type=execution.default_artifact_type,
                    ),
                )
                run.payload = {
                    **(run.payload or {}),
                    "intent_text": _request_intent_text(request),
                    "frame": {
                        "skill_match": framed.skill_match,
                        "artifact_type_hint": framed.artifact_type_hint,
                    },
                }
                await session.flush()

        workspace_dir = execution.workspace_root / str(run.id)
        workspace_dir.mkdir(parents=True, exist_ok=True)
        orchestrator = execution.orchestrator_factory(session)
        runner = AgentRunner(session)
        result = await runner.drive(
            run_id=run.id, orchestrator=orchestrator, workspace_dir=workspace_dir
        )
        logger.info(
            "agent_worker_driven",
            run_id=str(run.id),
            outcome=result.outcome,
        )

    async def _claim_batch(self, session: AsyncSession) -> AsyncIterator[RequestRow]:
        """Yield up to ``batch_size`` OPEN requests within ``session``."""
        stmt = (
            select(RequestRow)
            .where(RequestRow.status == RequestStatus.OPEN)
            .order_by(RequestRow.created_at.asc())
            .limit(self._cfg.batch_size)
            .with_for_update(skip_locked=True)
        )
        rows = (await session.execute(stmt)).scalars().all()
        for r in rows:
            yield r


def _request_intent_text(request: RequestRow) -> str:
    """Extract the founder's intent text from a Request payload."""
    payload = request.payload or {}
    if isinstance(payload, dict):
        for key in ("intent_text", "text", "title", "summary", "body", "message"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value
    return "Untitled run"


__all__ = ["AgentExecutionDeps", "AgentWorker", "AgentWorkerConfig"]
