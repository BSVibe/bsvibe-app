"""SafeModeExpirySweepRunner — system-wide Safe Mode queue expiry sweep.

D3 (PR #215) added Safe Mode queue lifecycle methods (``mark_delivered`` /
``archive`` / ``mark_deleted``) but left expiry unwired — a Safe Mode-gated
queue row past ``expires_at`` sat forever unless a per-workspace caller
explicitly invoked :meth:`SafeModeQueue.expire`. D3a closes the loop by
plugging the system-wide sweep into M1's
:class:`~backend.schedule.domain.runner_protocol.ScheduleRunnerProtocol` (PR #219).

**Why a custom runner, not a ``workspace_schedules`` row?** The
:class:`~backend.schedule.infrastructure.schedule_db.WorkspaceScheduleRow` schema requires
``workspace_id NOT NULL`` — a system-wide periodic sweep would either need
(a) widening that FK invariant for one sweep task or (b) a magic system-tenant
UUID. Both leak architectural debt for what is a small periodic chore. Instead,
:class:`SafeModeExpirySweepRunner` satisfies the **same**
:class:`ScheduleRunnerProtocol` the DB-poll runner does, so a SECOND
:class:`ScheduleWorker` instance can drive it on the same polling cadence the
DB-poll runner uses — honest reuse of M1's seam without bending the
``workspace_schedules`` invariant. The cron-algebra seam
(:class:`~backend.schedule.domain.advancer.ScheduleAdvancer`) is irrelevant here
(the sweep is "every tick", not a cron expression), so it isn't plumbed in.

**D3a vs Lift 0b boundary.** D3a's deliverable was the EXPIRY TRANSITION +
the audit hook. The transition flips ``PENDING/EXTENDED → EXPIRED`` via
:meth:`SafeModeQueue.mark_expired`, and ONE :class:`AuditOutboxRecord` per
non-empty batch records the provenance (``trigger=schedule``,
``source=system.safe_mode_expiry``).

D3b (PR #223) briefly added a per-item auto-compensation fan-out here, but
Lift 0b (v8 §13 / D7) rolled that wiring back as YAGNI — the only consumer
was the now-deleted ``backend.delivery.compensation`` module. The audit
row remains; any future audit-side subscriber can drive per-item logic
off the ``safe_mode.expired`` event_type without rejoining this sweep.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.workflow.application.safe_mode_queue import SafeModeQueue
from backend.workflow.channels import SAFE_MODE_QUEUE_ITEMS
from plugin.audit.store import OutboxStore

logger = structlog.get_logger(__name__)


SAFE_MODE_EXPIRED_EVENT_TYPE = "safe_mode.expired"
"""The audit event_type for the per-batch sweep record.

Stable wire string. Mirrors the ``backend/supervisor/audit/events.py``
``DEFAULT_EVENT_TYPE`` convention (``<domain>.<action>``); any future
audit-side subscriber matches on this exact string.
"""

SAFE_MODE_EXPIRY_SOURCE = "system.safe_mode_expiry"
"""The audit-payload ``source`` tag — identifies the sweep as the producer.

A founder reading the audit log can tell the expiry came from the SYSTEM
sweep (this exact source string), NOT from a per-workspace
:meth:`SafeModeQueue.expire` call or a user retract. The pairing
``trigger=schedule`` + ``source=system.safe_mode_expiry`` is the glass-box
contract any future audit subscriber filters on.
"""


class SafeModeExpirySweepRunner:
    """A :class:`ScheduleRunnerProtocol` impl that sweeps Safe Mode expiries.

    One :meth:`fire_due` call (one polling tick) opens ONE session, selects
    every ``PENDING / EXTENDED`` row whose ``expires_at <= now`` across ALL
    workspaces, transitions each via :meth:`SafeModeQueue.mark_expired`, and
    emits ONE :class:`AuditOutboxRecord` per non-empty batch — all in one
    transaction so a partial failure rolls the whole batch back (no half-
    expired state).

    The single audit row per BATCH (not per item) is deliberate: a thousand
    rows expiring in one tick is one operational event, not a thousand. The
    payload carries the full ``item_ids`` list so a founder can still
    cross-reference any specific item in the queue. Any future subscriber
    (e.g. a real notify-side handler) can match on the audit ``event_type``
    and fan out per-item there — the audit hook is the clean seam.

    ``now_fn`` lets tests inject a deterministic clock (the worker shell
    calls :meth:`fire_due` with its own wall-clock ``now`` argument, but the
    runner's ``now_fn`` wins so the per-row cutoff + the audit
    ``occurred_at`` come from ONE consistent clock — mirrors
    :class:`DbPollScheduleRunner`).
    """

    def __init__(
        self,
        *,
        now_fn: Callable[[], datetime] | None = None,
        outbox: OutboxStore | None = None,
    ) -> None:
        self._now_fn = now_fn or (lambda: datetime.now(tz=UTC))
        # The outbox store is dependency-injectable for tests; the default
        # is the same stateless façade the rest of the audit subsystem uses.
        self._outbox = outbox or OutboxStore()

    async def fire_due(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        now: datetime,  # noqa: ARG002 — Protocol contract; runner uses now_fn
    ) -> int:
        """Sweep every due Safe Mode queue row in ONE batch.

        Returns the number of rows that flipped to ``EXPIRED``. A zero
        return is a no-op tick (no rows past ``expires_at``) AND no audit
        emission, so the audit log stays truthful — the sweep ran but did
        nothing observable.
        """
        cutoff = self._now_fn()
        async with session_factory() as session:
            expired_ids = await self._sweep_one_batch(session, cutoff)
            if expired_ids:
                await self._emit_batch_audit(session, expired_ids=expired_ids, now=cutoff)
            await session.commit()
            return len(expired_ids)

    async def _sweep_one_batch(self, session: AsyncSession, cutoff: datetime) -> list[uuid.UUID]:
        """Transition every PENDING/EXTENDED row past ``cutoff`` to EXPIRED.

        Goes through :meth:`SafeModeQueue.mark_expired` per row (NOT a bulk
        UPDATE) so each transition runs the same enum-guard the per-item
        lifecycle methods do — a row that races into a settled state
        between the SELECT and the per-row update is skipped (returns
        ``False``), not silently regressed.

        Returns the list of successfully-transitioned ``item_id`` values
        for the audit emitter to record. (D3b briefly returned
        ``(item_id, deliverable_id)`` pairs to drive a per-item fan-out;
        Lift 0b reverted that — the audit row carries the item_ids as the
        cross-reference handle, and that's enough.)
        """
        queue = SafeModeQueue(session)
        due = await SAFE_MODE_QUEUE_ITEMS.consume(
            consumer_id="worker:safe_mode_expiry_sweep",
            claim=lambda: queue.list_due_expired(now=cutoff),
        )
        expired: list[uuid.UUID] = []
        for row in due:
            ok = await queue.mark_expired(workspace_id=row.workspace_id, item_id=row.id)
            if ok:
                expired.append(row.id)
        return expired

    async def _emit_batch_audit(
        self,
        session: AsyncSession,
        *,
        expired_ids: list[uuid.UUID],
        now: datetime,
    ) -> None:
        """Emit ONE :class:`AuditOutboxRecord` for the whole sweep batch.

        The wire shape mirrors :class:`AuditEventBase` (the typed envelope
        every other emitter uses): top-level ``event_id`` / ``event_type`` /
        ``occurred_at`` keys plus a ``data`` dict carrying the per-event
        fields. We use the lower-level :class:`OutboxStore` directly (vs.
        defining a typed event class) because the sweep emits at most one
        wire shape — the cost of a tiny typed model + its ``ClassVar`` ID
        would exceed the benefit. Any future subscriber matches on
        ``event_type == "safe_mode.expired"``; the ``data`` dict carries the
        forward-compatible payload.
        """
        event_id = uuid.uuid4()
        payload: dict[str, Any] = {
            "event_id": str(event_id),
            "event_type": SAFE_MODE_EXPIRED_EVENT_TYPE,
            "occurred_at": now.isoformat(),
            "actor": {"type": "system", "id": SAFE_MODE_EXPIRY_SOURCE},
            "data": {
                # Glass-box: a founder reading the audit log sees BOTH
                # ``trigger=schedule`` (came from the periodic sweep, NOT
                # a per-workspace expire / user retract) AND ``source=
                # system.safe_mode_expiry`` (the specific sweep, vs. other
                # schedule-driven events). Future subscribers filter on the
                # exact pair.
                "trigger": "schedule",
                "source": SAFE_MODE_EXPIRY_SOURCE,
                "count": len(expired_ids),
                "item_ids": [str(i) for i in expired_ids],
            },
        }
        await self._outbox.enqueue(
            session,
            producer_id="worker:safe_mode_expiry_sweep",
            event_id=str(event_id),
            event_type=SAFE_MODE_EXPIRED_EVENT_TYPE,
            occurred_at=now,
            payload=payload,
        )
        logger.info(
            "safe_mode_expiry_swept",
            count=len(expired_ids),
            trigger="schedule",
            source=SAFE_MODE_EXPIRY_SOURCE,
        )


__all__ = [
    "SAFE_MODE_EXPIRED_EVENT_TYPE",
    "SAFE_MODE_EXPIRY_SOURCE",
    "SafeModeExpirySweepRunner",
]
