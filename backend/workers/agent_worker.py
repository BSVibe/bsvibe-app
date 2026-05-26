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

import inspect
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.execution.db import ExecutionRun, RunStatus
from backend.execution.orchestrator import RunCompute
from backend.intake.db import RequestRow, RequestStatus
from backend.orchestrator.agent_runner import AgentRunner
from backend.orchestrator.frame import FrameConfig, FrameLlm, FrameStage
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

    * ``skill_loader_for`` — resolves a :class:`SkillLoader` rooted at the
      *run's* workspace skills directory (``<skills_root>/<workspace_id>/``).
      Skills are per-workspace (Workflow §6 #5), and a run's ``workspace_id``
      is only known per-run (inside :meth:`AgentWorker._frame_and_drive`), so
      this is a factory ``workspace_id -> SkillLoader`` rather than one shared
      loader — otherwise every workspace would frame against a single
      root-level skill set (a multi-tenancy scoping gap).
    * ``orchestrator_factory`` — builds a :class:`RunCompute` (the native
      :class:`~backend.execution.orchestrator.RunOrchestrator` for api-llm
      accounts, or the
      :class:`~backend.executors.orchestrator.ExecutorOrchestrator` for
      ``provider='executor'`` accounts) bound to the *same* session the run is
      driven in (so compute + transactional lifecycle share one transaction)
      AND to the *specific* run, so the factory can resolve the run's
      per-workspace work-LLM identity (the
      :class:`~backend.execution.db.ExecutionRun` carries only a
      ``workspace_id``; production resolves that workspace's active
      ModelAccount → ``account_id`` + ``model_account_id`` for the gateway
      work-LLM). It may also create a :class:`~backend.execution.db.Decision`
      and return ``None`` when the run cannot be resolved (e.g. zero / many
      active model accounts) — in which case ``drive_once`` skips driving the
      run, leaving it RUNNING (paused on the Decision, never silently stalled).
      Production injects the gateway work-LLM + real sandbox; tests inject the
      scripted LLM + Noop sandbox.
    * ``workspace_root`` — each run drives inside ``workspace_root/<run_id>``.
    * ``default_artifact_type`` — frame hint when no skill matches.
    """

    skill_loader_for: Callable[[uuid.UUID], SkillLoader]
    orchestrator_factory: Callable[
        [AsyncSession, ExecutionRun], RunCompute | Awaitable[RunCompute | None]
    ]
    workspace_root: Path
    default_artifact_type: str | None = "direct_output"
    #: B9a — the cheap-LLM framing seam, resolved per-workspace (mirrors the
    #: settle-extractor's gateway resolution). Either a static
    #: :class:`~backend.orchestrator.frame.FrameLlm`, or a factory
    #: ``(session, workspace_id) -> FrameLlm | None`` (sync or async) that
    #: resolves the workspace's active model account → a gateway cheap-LLM,
    #: BOUND to the worker's active framing session (so it shares the run's
    #: transaction, exactly like ``orchestrator_factory``). ``None`` (the
    #: default — executor-only / no account / legacy caller) makes
    #: :class:`~backend.orchestrator.frame.FrameStage` fall back to the keyword
    #: heuristic — the pre-B9a behaviour, no regression.
    frame_llm: (
        FrameLlm
        | Callable[[AsyncSession, uuid.UUID], FrameLlm | None | Awaitable[FrameLlm | None]]
        | None
    ) = None
    #: Optional hook to PROVISION the run's ``workspace_dir`` before the loop
    #: drives. ``None`` (the default) keeps the existing behaviour: the run
    #: drives in an EMPTY scratch dir (``workspace_root/<run_id>``) — exactly as
    #: the Direct-path tests rely on. When set, it is awaited with
    #: ``(session, run, workspace_dir)`` AFTER the dir is created, BEFORE the
    #: loop drives. The github delivery path injects a provisioner that resolves
    #: the run's workspace github connector binding and, when present, CLONES the
    #: target repo into ``workspace_dir`` on a new ``bsvibe/run-<short id>``
    #: branch — so the agent's file_write/file_edit operate on a real checkout a
    #: PR diff can be built from. No github binding → the provisioner leaves the
    #: empty dir untouched (non-github runs are unaffected).
    workspace_provisioner: Callable[[AsyncSession, ExecutionRun, Path], Awaitable[None]] | None = (
        None
    )


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
                # Per-workspace skill scoping: frame against the loader rooted
                # at THIS run's ``<skills_root>/<workspace_id>/`` (Workflow §6 #5),
                # not a single shared root-level set.
                skill_loader = execution.skill_loader_for(run.workspace_id)
                # B9a — resolve the per-workspace cheap-LLM for real framing,
                # bound to this framing session. None (executor-only / no
                # account) → keyword fallback.
                frame_llm = await _resolve_frame_llm(execution, session, run.workspace_id)
                framed = await self._frame_stage.frame(
                    request=request,
                    config=FrameConfig(
                        skill_loader=skill_loader,
                        default_artifact_type=execution.default_artifact_type,
                        llm=frame_llm,
                    ),
                )
                # Record the FULL framing (B9a): skill match + artifact-type hint
                # (for delivery routing) + the refined intent + the path
                # classification (recorded for B9b, which acts on knowledge_only).
                run.payload = {
                    **(run.payload or {}),
                    "intent_text": _request_intent_text(request),
                    "frame": {
                        "skill_match": framed.skill_match,
                        "artifact_type_hint": framed.artifact_type_hint,
                        "framed_intent": framed.framed_intent,
                        "path_classification": framed.path_classification,
                    },
                }
                await session.flush()

        runner = AgentRunner(session)
        orchestrator = await _resolve_orchestrator(execution, session, run)
        if orchestrator is None:
            # Factory could not resolve the run (e.g. created a Decision for
            # zero/ambiguous model accounts). Transition the run to RUNNING so
            # it is paused on the Decision — NOT re-picked by the next
            # ``drive_once`` (which scans OPEN runs), so no duplicate Decision
            # is minted each tick. Mirrors the orchestrator's needs_decision
            # semantics (run stays RUNNING, never silently stalled).
            await runner.transition(
                run_id=run.id,
                to_status=RunStatus.RUNNING,
                reason="paused on decision: model account unresolved",
            )
            logger.info("agent_worker_run_unresolved", run_id=str(run.id))
            return

        workspace_dir = execution.workspace_root / str(run.id)
        workspace_dir.mkdir(parents=True, exist_ok=True)
        if execution.workspace_provisioner is not None:
            # github delivery path: clone the target repo into workspace_dir on
            # a new branch so the agent's file edits build a real PR diff. No
            # github binding → the provisioner is a no-op and the empty scratch
            # dir is used exactly as the non-github path (Direct-path tests).
            await execution.workspace_provisioner(session, run, workspace_dir)
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


async def _resolve_orchestrator(
    execution: AgentExecutionDeps, session: AsyncSession, run: ExecutionRun
) -> RunCompute | None:
    """Call ``orchestrator_factory`` supporting both sync and async factories.

    The narrow Phase 1 factory was ``(session) -> RunOrchestrator``; Phase 2
    widens it to ``(session, run) -> RunCompute | None`` and additionally
    permits an async factory (production resolution hits the DB). This shim
    awaits the result when the factory is a coroutine."""
    produced = execution.orchestrator_factory(session, run)
    if inspect.isawaitable(produced):
        return await produced
    return produced


async def _resolve_frame_llm(
    execution: AgentExecutionDeps, session: AsyncSession, workspace_id: uuid.UUID
) -> FrameLlm | None:
    """Resolve the per-workspace cheap-LLM for framing, or ``None`` to fall back.

    ``execution.frame_llm`` may be a static :class:`FrameLlm`, a sync factory, or
    an async factory ``(session, workspace_id) -> FrameLlm | None``. A static
    instance (one that exposes ``complete_text``) is returned as-is; a callable
    is invoked with the framing session + workspace id (awaited when it returns a
    coroutine). ``None`` anywhere → the keyword fallback (no regression for
    executor-only / accountless workspaces)."""
    frame_llm = execution.frame_llm
    if frame_llm is None:
        return None
    # A static FrameLlm satisfies the Protocol (has ``complete_text``); a factory
    # does not — distinguish on that rather than ``callable`` (the Protocol stub
    # may itself be callable).
    if isinstance(frame_llm, FrameLlm):
        return frame_llm
    produced = frame_llm(session, workspace_id)
    if inspect.isawaitable(produced):
        return await produced
    return produced


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
