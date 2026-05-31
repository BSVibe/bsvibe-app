"""SafeModeExpirySweep — D3a system-wide queue expiry sweep, plugged into
the M1 :class:`ScheduleRunnerProtocol`.

D3 (PR #215) added Safe Mode lifecycle methods ``mark_delivered`` / ``archive``
/ ``mark_deleted`` but the expiry sweep was unwired — a Safe Mode-gated
queue item past ``expires_at`` sat forever. M1 (PR #219) shipped the
swappable :class:`ScheduleRunnerProtocol` substrate; D3a plugs the sweep
into that seam.

Proves the four deltas attributable to this lift:

1. **Expiry transition fires.** A ``safe_mode_queue_items`` row with
   ``expires_at <= now`` flips to ``EXPIRED`` within one polling tick of
   the schedule runner. Asserted at the lifecycle-method site (the row's
   ``status`` column), against real Postgres.
2. **Pre-expiry no-op.** A queue row with ``expires_at > now`` is NOT
   touched by the sweep. Complements #1 — the runner doesn't blindly
   sweep every pending row.
3. **Idempotence.** Running the sweep twice on the same expired row does
   not double-fire — an already-``EXPIRED`` row stays expired AND the
   audit-outbox does not duplicate. Tested at the lifecycle-method site.
4. **Glass-box.** The spawned :class:`AuditOutboxRecord` carries
   ``payload["trigger"] == "schedule"`` AND
   ``payload["source"] == "system.safe_mode_expiry"`` so a founder can
   tell the expiry came from the sweep, not a user retract or a
   per-workspace ``expire`` call.

The sweep does NOT auto-fire compensation — that's D3b (next PR). D3a's
deliverable is just the expiry transition + the audit hook so D3b can
subscribe to it cleanly.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.workers.schedule_runner import ScheduleWorker
from backend.workflow.application.safe_mode_expiry import SafeModeExpirySweepRunner
from backend.workflow.application.safe_mode_queue import SafeModeQueue
from backend.workflow.infrastructure.delivery.db import (
    DeliveryBase,
    SafeModeQueueItemRow,
    SafeModeStatus,
)
from plugin.audit.models import AuditOutboxRecord

from .._support import db_engine

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def sf() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with db_engine(DeliveryBase) as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


async def _enqueue_with_expiry(
    sf: async_sessionmaker[AsyncSession],
    *,
    workspace_id: uuid.UUID,
    expires_at: datetime,
    status: SafeModeStatus = SafeModeStatus.PENDING,
) -> uuid.UUID:
    """Seed a queue row with a custom ``expires_at`` (bypassing the default 90d
    enqueue) so the sweep has something past/future to find."""
    item_id = uuid.uuid4()
    async with sf() as s:
        s.add(
            SafeModeQueueItemRow(
                id=item_id,
                workspace_id=workspace_id,
                deliverable_id=uuid.uuid4(),
                run_id=None,
                status=status,
                expires_at=expires_at,
                extension_count=0,
            )
        )
        await s.commit()
    return item_id


# ---------------------------------------------------------------------------
# Delta 1: expiry transition fires — past expiry flips PENDING → EXPIRED
# ---------------------------------------------------------------------------


async def test_past_expiry_flips_to_expired_within_one_tick(
    sf: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id = uuid.uuid4()
    now = datetime(2026, 5, 25, 12, 0, 0, tzinfo=UTC)
    item_id = await _enqueue_with_expiry(
        sf,
        workspace_id=workspace_id,
        expires_at=now - timedelta(seconds=1),  # past
    )

    runner = SafeModeExpirySweepRunner(now_fn=lambda: now)
    worker = ScheduleWorker(session_factory=sf, runner=runner, name="safe_mode_expiry_worker")

    expired = await worker.fire_due_once()

    assert expired == 1, "the one past-expiry row should fire in this tick"

    async with sf() as s:
        row = await s.get(SafeModeQueueItemRow, item_id)
    assert row is not None
    assert row.status is SafeModeStatus.EXPIRED


async def test_extended_state_also_expires(sf: async_sessionmaker[AsyncSession]) -> None:
    """An ``EXTENDED`` row past its (extended) ``expires_at`` also expires —
    extension is just a longer window, not an exemption."""
    workspace_id = uuid.uuid4()
    now = datetime(2026, 5, 25, 12, 0, 0, tzinfo=UTC)
    item_id = await _enqueue_with_expiry(
        sf,
        workspace_id=workspace_id,
        expires_at=now - timedelta(seconds=1),
        status=SafeModeStatus.EXTENDED,
    )

    runner = SafeModeExpirySweepRunner(now_fn=lambda: now)
    worker = ScheduleWorker(session_factory=sf, runner=runner, name="safe_mode_expiry_worker")
    expired = await worker.fire_due_once()
    assert expired == 1

    async with sf() as s:
        row = await s.get(SafeModeQueueItemRow, item_id)
    assert row is not None
    assert row.status is SafeModeStatus.EXPIRED


# ---------------------------------------------------------------------------
# Delta 2: pre-expiry no-op — future expires_at is not touched
# ---------------------------------------------------------------------------


async def test_future_expiry_is_not_touched(sf: async_sessionmaker[AsyncSession]) -> None:
    workspace_id = uuid.uuid4()
    now = datetime(2026, 5, 25, 12, 0, 0, tzinfo=UTC)
    item_id = await _enqueue_with_expiry(
        sf,
        workspace_id=workspace_id,
        expires_at=now + timedelta(hours=1),  # future
    )

    runner = SafeModeExpirySweepRunner(now_fn=lambda: now)
    worker = ScheduleWorker(session_factory=sf, runner=runner, name="safe_mode_expiry_worker")
    expired = await worker.fire_due_once()
    assert expired == 0

    async with sf() as s:
        row = await s.get(SafeModeQueueItemRow, item_id)
    assert row is not None
    assert row.status is SafeModeStatus.PENDING, "future-expiry row must stay pending"


async def test_terminal_states_are_not_touched(sf: async_sessionmaker[AsyncSession]) -> None:
    """Approved / Denied / Delivered / Archived / Deleted rows past
    ``expires_at`` are NOT swept — only PENDING / EXTENDED are sweepable.
    Defends against an accidental sweep regressing a settled item back to
    ``EXPIRED``."""
    workspace_id = uuid.uuid4()
    now = datetime(2026, 5, 25, 12, 0, 0, tzinfo=UTC)
    past = now - timedelta(seconds=1)

    item_ids: dict[SafeModeStatus, uuid.UUID] = {}
    for status in (
        SafeModeStatus.APPROVED,
        SafeModeStatus.DENIED,
        SafeModeStatus.DELIVERED,
        SafeModeStatus.ARCHIVED,
        SafeModeStatus.DELETED,
    ):
        item_ids[status] = await _enqueue_with_expiry(
            sf, workspace_id=workspace_id, expires_at=past, status=status
        )

    runner = SafeModeExpirySweepRunner(now_fn=lambda: now)
    worker = ScheduleWorker(session_factory=sf, runner=runner, name="safe_mode_expiry_worker")
    expired = await worker.fire_due_once()
    assert expired == 0

    async with sf() as s:
        for status, item_id in item_ids.items():
            row = await s.get(SafeModeQueueItemRow, item_id)
            assert row is not None
            assert row.status is status, f"{status.value} must not be swept to EXPIRED"


# ---------------------------------------------------------------------------
# Delta 3: idempotence — second tick neither re-flips nor double-audits
# ---------------------------------------------------------------------------


async def test_second_tick_does_not_double_fire(sf: async_sessionmaker[AsyncSession]) -> None:
    """A second sweep tick at the same clock must not re-fire the already
    expired row OR duplicate the audit-outbox row. The first tick already
    flipped PENDING → EXPIRED; the second sees nothing PENDING/EXTENDED
    past ``expires_at`` and returns zero."""
    workspace_id = uuid.uuid4()
    now = datetime(2026, 5, 25, 12, 0, 0, tzinfo=UTC)
    item_id = await _enqueue_with_expiry(
        sf,
        workspace_id=workspace_id,
        expires_at=now - timedelta(seconds=1),
    )

    runner = SafeModeExpirySweepRunner(now_fn=lambda: now)
    worker = ScheduleWorker(session_factory=sf, runner=runner, name="safe_mode_expiry_worker")

    first = await worker.fire_due_once()
    second = await worker.fire_due_once()

    assert first == 1, "first tick should fire the past-expiry row"
    assert second == 0, "second tick on already-expired row must be a no-op"

    # Row still EXPIRED (not re-flipped to anything else):
    async with sf() as s:
        row = await s.get(SafeModeQueueItemRow, item_id)
    assert row is not None
    assert row.status is SafeModeStatus.EXPIRED

    # Audit-outbox: exactly one ``safe_mode.expired`` record total.
    async with sf() as s:
        records = (
            (
                await s.execute(
                    select(AuditOutboxRecord).where(
                        AuditOutboxRecord.event_type == "safe_mode.expired"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(records) == 1, "second tick must not duplicate the audit row"


# ---------------------------------------------------------------------------
# Delta 4: glass-box — trigger=schedule, source=system.safe_mode_expiry
# ---------------------------------------------------------------------------


async def test_sweep_emits_audit_with_schedule_provenance(
    sf: async_sessionmaker[AsyncSession],
) -> None:
    """A successful sweep must emit an :class:`AuditOutboxRecord` whose
    payload carries the schedule provenance tags so a founder reading the
    audit log can tell the expiry came from the sweep (not a user
    retract). Lock the tags at the emitter site so a future Redis-Streams
    runner can't silently drop them."""
    workspace_id = uuid.uuid4()
    now = datetime(2026, 5, 25, 12, 0, 0, tzinfo=UTC)
    item_id = await _enqueue_with_expiry(
        sf,
        workspace_id=workspace_id,
        expires_at=now - timedelta(seconds=1),
    )

    runner = SafeModeExpirySweepRunner(now_fn=lambda: now)
    worker = ScheduleWorker(session_factory=sf, runner=runner, name="safe_mode_expiry_worker")
    await worker.fire_due_once()

    async with sf() as s:
        record = (
            (
                await s.execute(
                    select(AuditOutboxRecord).where(
                        AuditOutboxRecord.event_type == "safe_mode.expired"
                    )
                )
            )
            .scalars()
            .one()
        )
    payload = record.payload
    data = payload.get("data") if isinstance(payload, dict) else None
    assert isinstance(data, dict)
    assert data.get("trigger") == "schedule"
    assert data.get("source") == "system.safe_mode_expiry"
    # Provenance: the expired item IDs are surfaced so a founder can
    # cross-reference the queue.
    item_ids = data.get("item_ids")
    assert isinstance(item_ids, list)
    assert str(item_id) in item_ids
    assert data.get("count") == 1


async def test_sweep_with_no_expired_emits_no_audit(
    sf: async_sessionmaker[AsyncSession],
) -> None:
    """A sweep tick that finds nothing past ``expires_at`` must NOT emit a
    spurious audit row — keeps the audit log truthful (the sweep ran but
    did nothing)."""
    workspace_id = uuid.uuid4()
    now = datetime(2026, 5, 25, 12, 0, 0, tzinfo=UTC)
    await _enqueue_with_expiry(
        sf,
        workspace_id=workspace_id,
        expires_at=now + timedelta(hours=1),  # future
    )

    runner = SafeModeExpirySweepRunner(now_fn=lambda: now)
    worker = ScheduleWorker(session_factory=sf, runner=runner, name="safe_mode_expiry_worker")
    fired = await worker.fire_due_once()
    assert fired == 0

    async with sf() as s:
        records = (
            (
                await s.execute(
                    select(AuditOutboxRecord).where(
                        AuditOutboxRecord.event_type == "safe_mode.expired"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert records == []


# ---------------------------------------------------------------------------
# Cross-workspace sweep — the runner doesn't need a workspace filter
# ---------------------------------------------------------------------------


async def test_sweep_is_system_wide_across_workspaces(
    sf: async_sessionmaker[AsyncSession],
) -> None:
    """Unlike :meth:`SafeModeQueue.expire` (per-workspace), the sweep is
    system-wide: a single tick sweeps every workspace's expired rows. This
    is the property that makes Option A' work without a system-tenant
    workspace_id on ``workspace_schedules``."""
    ws_a = uuid.uuid4()
    ws_b = uuid.uuid4()
    now = datetime(2026, 5, 25, 12, 0, 0, tzinfo=UTC)
    a_id = await _enqueue_with_expiry(sf, workspace_id=ws_a, expires_at=now - timedelta(seconds=1))
    b_id = await _enqueue_with_expiry(sf, workspace_id=ws_b, expires_at=now - timedelta(seconds=1))

    runner = SafeModeExpirySweepRunner(now_fn=lambda: now)
    worker = ScheduleWorker(session_factory=sf, runner=runner, name="safe_mode_expiry_worker")
    expired = await worker.fire_due_once()
    assert expired == 2

    async with sf() as s:
        for item_id in (a_id, b_id):
            row = await s.get(SafeModeQueueItemRow, item_id)
            assert row is not None
            assert row.status is SafeModeStatus.EXPIRED


# ---------------------------------------------------------------------------
# Lifecycle method — SafeModeQueue.mark_expired (used by the sweep)
# ---------------------------------------------------------------------------


async def test_mark_expired_flips_pending_to_expired(
    sf: async_sessionmaker[AsyncSession],
) -> None:
    """The single-item :meth:`SafeModeQueue.mark_expired` method that the
    sweep delegates to: PENDING → EXPIRED. Exposed for testability +
    symmetry with the rest of the lifecycle vocabulary (``mark_delivered``
    / ``archive`` / ``mark_deleted``)."""
    workspace_id = uuid.uuid4()
    now = datetime(2026, 5, 25, 12, 0, 0, tzinfo=UTC)
    item_id = await _enqueue_with_expiry(
        sf,
        workspace_id=workspace_id,
        expires_at=now - timedelta(seconds=1),
    )

    async with sf() as s:
        q = SafeModeQueue(s)
        ok = await q.mark_expired(workspace_id=workspace_id, item_id=item_id)
        await s.commit()
    assert ok is True

    async with sf() as s:
        row = await s.get(SafeModeQueueItemRow, item_id)
    assert row is not None
    assert row.status is SafeModeStatus.EXPIRED


async def test_mark_expired_rejects_non_pending(
    sf: async_sessionmaker[AsyncSession],
) -> None:
    """``mark_expired`` only fires on PENDING / EXTENDED rows — already
    approved/denied/delivered items must not regress."""
    workspace_id = uuid.uuid4()
    now = datetime(2026, 5, 25, 12, 0, 0, tzinfo=UTC)
    item_id = await _enqueue_with_expiry(
        sf,
        workspace_id=workspace_id,
        expires_at=now - timedelta(seconds=1),
        status=SafeModeStatus.APPROVED,
    )

    async with sf() as s:
        q = SafeModeQueue(s)
        ok = await q.mark_expired(workspace_id=workspace_id, item_id=item_id)
        await s.commit()
    assert ok is False

    async with sf() as s:
        row = await s.get(SafeModeQueueItemRow, item_id)
    assert row is not None
    assert row.status is SafeModeStatus.APPROVED
