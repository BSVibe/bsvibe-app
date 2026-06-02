"""AuditRetentionSweepRunner — per-workspace ``audit_outbox`` retention sweep.

Lift Q1. The founder-locked roadmap §6 결정 로그 Q1 default is *forever*
retention; the architectural deliverable is the per-workspace knob a
workspace can use to opt INTO N-day rotation. The column is
:attr:`backend.identity.workspaces_db.WorkspaceRow.audit_retention_days`
(``INTEGER NULL`` — ``NULL`` = forever, ``N >= 1`` = rotate after N
days). This module ships the daily sweep that actually deletes rows
past that workspace's retention.

**Why a custom runner, not a ``workspace_schedules`` row?** Same answer
as :class:`backend.workflow.application.safe_mode_expiry.SafeModeExpirySweepRunner`
— a system-wide periodic sweep doesn't fit the ``workspace_id NOT NULL``
invariant on the schedule table. Instead this runner satisfies the
SAME :class:`~backend.schedule.domain.runner_protocol.ScheduleRunnerProtocol`
so a THIRD :class:`ScheduleWorker` instance can drive it on a daily
cadence — honest reuse of M1's seam.

**Why does the sweep live in the AUDIT plugin (not workflow)?** The
``audit_outbox`` table is the audit plugin's storage; the retention
policy is *its* concern. The sweep only reaches across boundaries to
read the :class:`WorkspaceRepository` (to get each workspace's
``audit_retention_days`` value), which is the same kind of cross-context
read the existing :mod:`plugin.audit.subscriber` already does (bridges
into ``backend.api.v1.live_events``). ``plugin.audit`` is the in-tree
transactional-outbox subscriber, NOT a connector plugin — it is
explicitly carved out of the strict plugin import-linter contract.

**Retention semantics — hard delete, regardless of delivery state.**
Rows past ``occurred_at < now - retention_days * 1d`` are deleted
whether ``delivered_at`` is set or not, including ``dead_letter``
rows. The assumption: anything not delivered after N days (the founder's
configured retention) is dead — keeping it in the outbox indefinitely
would defeat the rotation. This is the documented hard contract on
``audit_retention_days``; a workspace that needs different semantics
keeps the column ``NULL`` (= forever).

**Per-batch audit row.** Mirrors the SafeMode expiry sweep — ONE
``audit.retention.swept`` :class:`AuditOutboxRecord` per workspace per
non-empty batch, tagged ``trigger=schedule`` and
``source=system.audit_retention``. A founder reading the audit log can
tell the deletion came from the sweep (not a manual prune), and any
future subscriber can match on the exact event_type string. This row
itself is subject to retention on the NEXT sweep — that's intentional
(it ages out with the rest).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.identity.domain.repositories import WorkspaceRepository
from backend.identity.infrastructure.repositories import SqlAlchemyWorkspaceRepository
from plugin.audit.models import AuditOutboxRecord
from plugin.audit.store import OutboxStore

logger = structlog.get_logger(__name__)


AUDIT_RETENTION_SWEPT_EVENT_TYPE = "audit.retention.swept"
"""Stable wire string for the per-batch sweep record.

A future subscriber filters on ``event_type == "audit.retention.swept"``.
"""

AUDIT_RETENTION_SOURCE = "system.audit_retention"
"""The audit-payload ``source`` tag identifying the sweep as the producer."""


WorkspaceRepoFactory = Callable[[AsyncSession], WorkspaceRepository]


def _default_repo_factory(session: AsyncSession) -> WorkspaceRepository:
    return SqlAlchemyWorkspaceRepository(session)


class AuditRetentionSweepRunner:
    """A :class:`ScheduleRunnerProtocol` impl that sweeps audit_outbox per workspace.

    One :meth:`fire_due` call (one polling tick) opens ONE session, lists
    every workspace with a non-NULL ``audit_retention_days``, and for each
    one DELETEs ``audit_outbox`` rows whose
    ``occurred_at < now - retention_days * 1d`` AND whose payload carries
    that workspace_id. ONE batch audit row per workspace per non-empty
    delete is emitted into the SAME outbox in the SAME transaction so a
    rollback rolls back BOTH the deletion and the audit hook.

    The returned int is the TOTAL row count deleted across all workspaces
    in this tick (NOT the workspace count) — matches the
    ``rows_affected`` operational signal callers care about. A zero
    return is a no-op tick (no workspace had retention OR no rows past
    retention) AND no audit emission.

    ``now_fn`` lets tests inject a deterministic clock; the worker shell
    calls :meth:`fire_due` with its own wall-clock ``now`` argument, but
    the runner's ``now_fn`` wins so the per-workspace cutoff + the audit
    ``occurred_at`` come from ONE consistent clock — mirrors
    :class:`SafeModeExpirySweepRunner` / :class:`DbPollScheduleRunner`.
    """

    def __init__(
        self,
        *,
        now_fn: Callable[[], datetime] | None = None,
        outbox: OutboxStore | None = None,
        workspace_repo_factory: WorkspaceRepoFactory | None = None,
    ) -> None:
        self._now_fn = now_fn or (lambda: datetime.now(tz=UTC))
        self._outbox = outbox or OutboxStore()
        self._workspace_repo_factory = workspace_repo_factory or _default_repo_factory

    async def fire_due(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        now: datetime,  # noqa: ARG002 — Protocol contract; runner uses now_fn
    ) -> int:
        """Sweep every workspace's audit_outbox in ONE batch."""
        sweep_at = self._now_fn()
        total_deleted = 0
        async with session_factory() as session:
            repo = self._workspace_repo_factory(session)
            rows = await repo.list_with_audit_retention()
            for workspace_id, retention_days in rows:
                if retention_days < 1:
                    # Defense-in-depth: REST validates ``>=1``, but a stray
                    # 0/negative on the row should be a no-op, not a wipe.
                    continue
                deleted = await self._sweep_one_workspace(
                    session,
                    workspace_id=workspace_id,
                    retention_days=retention_days,
                    now=sweep_at,
                )
                if deleted:
                    await self._emit_workspace_audit(
                        session,
                        workspace_id=workspace_id,
                        retention_days=retention_days,
                        deleted_count=deleted,
                        now=sweep_at,
                    )
                    total_deleted += deleted
            await session.commit()
        if total_deleted:
            logger.info(
                "audit_retention_swept",
                total_deleted=total_deleted,
                trigger="schedule",
                source=AUDIT_RETENTION_SOURCE,
            )
        return total_deleted

    async def _sweep_one_workspace(
        self,
        session: AsyncSession,
        *,
        workspace_id: uuid.UUID,
        retention_days: int,
        now: datetime,
    ) -> int:
        """Delete ``audit_outbox`` rows past this workspace's retention."""
        cutoff = now - timedelta(days=retention_days)
        stmt = delete(AuditOutboxRecord).where(
            AuditOutboxRecord.payload["workspace_id"].as_string() == str(workspace_id),
            AuditOutboxRecord.occurred_at < cutoff,
        )
        result = await session.execute(stmt)
        # ``session.execute(delete(...))`` returns a ``CursorResult`` (not the
        # generic ``Result``) whose ``rowcount`` carries the DELETE row count.
        # The :class:`sqlalchemy.ext.asyncio.AsyncSession.execute` typing
        # widens it to ``Result``; the runtime object is always the cursor
        # variant for a DML statement.
        rowcount = getattr(result, "rowcount", None)
        return int(rowcount or 0)

    async def _emit_workspace_audit(
        self,
        session: AsyncSession,
        *,
        workspace_id: uuid.UUID,
        retention_days: int,
        deleted_count: int,
        now: datetime,
    ) -> None:
        """Emit ONE :class:`AuditOutboxRecord` for this workspace's batch.

        Wire shape mirrors :class:`AuditEventBase` (top-level
        ``event_id`` / ``event_type`` / ``occurred_at`` / ``actor`` keys
        plus a ``data`` dict carrying the per-event fields). We use
        :class:`OutboxStore` directly (vs. a typed event class) for the
        same reason the SafeMode expiry sweep does — one wire shape, no
        need for a typed envelope.

        Critically, the audit row's payload INCLUDES the
        ``workspace_id`` so it ages out under THIS workspace's retention
        on the next sweep (the row carrying the deletion record is itself
        deletable — that's intentional and consistent).
        """
        event_id = uuid.uuid4()
        cutoff = now - timedelta(days=retention_days)
        payload: dict[str, Any] = {
            "event_id": str(event_id),
            "event_type": AUDIT_RETENTION_SWEPT_EVENT_TYPE,
            "occurred_at": now.isoformat(),
            "workspace_id": str(workspace_id),
            "actor": {"type": "system", "id": AUDIT_RETENTION_SOURCE},
            "data": {
                "trigger": "schedule",
                "source": AUDIT_RETENTION_SOURCE,
                "retention_days": retention_days,
                "cutoff": cutoff.isoformat(),
                "deleted_count": deleted_count,
            },
        }
        await self._outbox.insert(
            session,
            event_id=str(event_id),
            event_type=AUDIT_RETENTION_SWEPT_EVENT_TYPE,
            occurred_at=now,
            payload=payload,
        )


__all__ = [
    "AUDIT_RETENTION_SOURCE",
    "AUDIT_RETENTION_SWEPT_EVENT_TYPE",
    "AuditRetentionSweepRunner",
]
