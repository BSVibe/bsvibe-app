"""RetractionSweepRunner — the background sweep the retract queue always claimed.

``RetractionService.apply_pending`` writes a queued retract's ``retracted_at``
tombstone once its 30-second undo window closes. Two places described a sweep
that drives it — the service's own module docstring ("the next worker tick") and
the REST handler ("via ``apply_pending`` from the next call / background
sweep") — and ``ontology_corrections`` even carries an index annotated
``"Sweep / lazy-resolve query"``.

There was no sweep. Measured 2026-08-26: the only production caller was
``backend/mcp/tools/knowledge_tools.py``, on four MCP READ tools, whose docstring
says it outright — "The retract queue has no background sweep; the tombstone is
written on the *next call that knows the workspace*."

So the tombstone landed only when an AGENT happened to read the garden over MCP.
prod's 1,277 retractions are all applied for exactly that reason, not by design.
A founder working from the PWA issues a retract and the vault is never stamped —
the note keeps grounding answers until something unrelated happens to run.

This is the fourth runner on the SAME :class:`ScheduleRunnerProtocol` seam the
repo already sweeps with (schedule fire, safe-mode expiry, audit retention). No
new mechanism: a runner plus one registration in ``worker_runtime``.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.knowledge.application.retraction_service import RetractionService, TombstoneWriter
from backend.knowledge.infrastructure.ontology_db import OntologyCorrection

logger = structlog.get_logger(__name__)

#: Cap on workspaces handled per tick. A sweep that fans out over every
#: workspace in one transaction would hold the pool for as long as the slowest
#: vault write; the rest are picked up on the next tick (the row IS the timer,
#: so nothing is lost by deferring).
_MAX_WORKSPACES_PER_TICK = 50


class RetractionSweepRunner:
    """Apply every retract whose undo window has closed, in every workspace.

    Satisfies :class:`~backend.schedule.domain.runner_protocol.ScheduleRunnerProtocol`
    so it plugs into a :class:`ScheduleWorker` exactly as the safe-mode expiry and
    audit-retention sweeps do.

    ``writer_factory`` builds the per-workspace vault writer — injected rather
    than constructed here so this stays testable without a vault on disk, the
    same shape the MCP call site uses (``GardenWriter(vault=…)``). ASYNC because
    rooting a vault needs the workspace's REGION, which is a DB read.
    """

    __slots__ = ("_writer_factory",)

    def __init__(
        self, *, writer_factory: Callable[[uuid.UUID], Awaitable[TombstoneWriter]]
    ) -> None:
        self._writer_factory = writer_factory

    async def fire_due(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        now: object = None,
    ) -> int:
        """One sweep batch. Returns the number of corrections actually applied."""
        workspaces = await self._workspaces_with_pending(session_factory)
        if not workspaces:
            return 0

        applied_total = 0
        for workspace_id in workspaces:
            try:
                async with session_factory() as session:
                    service = RetractionService(
                        session=session, writer=await self._writer_factory(workspace_id)
                    )
                    applied = await service.apply_pending(workspace_id=workspace_id)
                    if applied:
                        await session.commit()
                        applied_total += applied
            except Exception:  # noqa: BLE001 — one bad workspace must not stop the sweep
                logger.warning(
                    "retraction_sweep_workspace_failed",
                    workspace_id=str(workspace_id),
                    exc_info=True,
                )
        if applied_total:
            logger.info(
                "retraction_sweep_applied",
                applied=applied_total,
                workspaces=len(workspaces),
            )
        return applied_total

    @staticmethod
    async def _workspaces_with_pending(
        session_factory: async_sessionmaker[AsyncSession],
    ) -> list[uuid.UUID]:
        """Workspaces holding at least one in-flight correction.

        Deliberately NOT filtered on ``apply_at`` here: ``apply_pending`` owns
        the deadline check (it is the same predicate whether it fires from a
        read, a REST tail, or this sweep), and duplicating it would give the
        window two definitions that can drift apart.
        """
        async with session_factory() as session:
            rows = await session.execute(
                select(OntologyCorrection.workspace_id)
                .where(
                    OntologyCorrection.applied_at.is_(None),
                    OntologyCorrection.cancelled_at.is_(None),
                )
                .distinct()
                .limit(_MAX_WORKSPACES_PER_TICK)
            )
            return [uuid.UUID(str(w)) for w in rows.scalars().all()]


__all__ = ["RetractionSweepRunner"]
