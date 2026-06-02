"""AuditRetentionSweep — Lift Q1 per-workspace ``audit_outbox`` retention sweep,
plugged into the M1 :class:`ScheduleRunnerProtocol`.

Lift Q1 (roadmap §6 결정 로그 Q1) shipped the founder-locked default
"forever retention" + the per-workspace ``audit_retention_days`` knob.
This test pins the SIX deltas attributable to the sweep that the column
empowers:

1. **Past-cutoff row deletes.** A workspace with ``audit_retention_days=N``
   loses every ``audit_outbox`` row whose ``occurred_at < now - N*1d``,
   within one polling tick. Asserted against real Postgres.
2. **Within-cutoff row survives.** A row inside the retention window is
   NOT touched by the sweep — complements #1 (no blind wipe).
3. **NULL-retention workspace is skipped.** A workspace with
   ``audit_retention_days = NULL`` (= forever, the default) loses
   nothing, even if its rows are years old. The skip happens at the
   repository level — the sweep never sees NULL-retention workspaces.
4. **Multi-workspace isolation.** Workspace A's retention doesn't touch
   workspace B's rows. The sweep's per-workspace filter is keyed on the
   payload's ``workspace_id`` field.
5. **Glass-box.** The spawned :class:`AuditOutboxRecord` carries
   ``payload['data']['trigger'] == 'schedule'`` AND
   ``payload['data']['source'] == 'system.audit_retention'`` so a
   founder can tell the deletion came from the sweep.
6. **Hard delete regardless of delivery state.** A row past cutoff is
   deleted even if ``delivered_at`` is NULL — the documented hard
   contract on ``audit_retention_days``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.identity.workspaces_db import WorkspaceRow
from backend.schedule.infrastructure.workers.schedule_worker import ScheduleWorker
from plugin.audit.models import AuditOutboxRecord
from plugin.audit.retention_sweep import (
    AUDIT_RETENTION_SOURCE,
    AUDIT_RETENTION_SWEPT_EVENT_TYPE,
    AuditRetentionSweepRunner,
)

from .._support import db_engine

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def sf() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    # No metadata args → unified ``Base.metadata`` (covers WorkspaceRow + AuditOutboxRecord).
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


async def _seed_workspace(
    sf: async_sessionmaker[AsyncSession],
    *,
    name: str,
    audit_retention_days: int | None,
) -> uuid.UUID:
    workspace_id = uuid.uuid4()
    async with sf() as s:
        s.add(WorkspaceRow(id=workspace_id, name=name, audit_retention_days=audit_retention_days))
        await s.commit()
    return workspace_id


async def _seed_outbox_row(
    sf: async_sessionmaker[AsyncSession],
    *,
    workspace_id: uuid.UUID,
    occurred_at: datetime,
    delivered: bool = False,
) -> str:
    """Seed one ``audit_outbox`` row whose payload carries ``workspace_id``.

    Returns the seeded row's ``event_id`` (UNIQUE-stable across the test) —
    NOT the bigint ``id``, because SQLite reuses autoincrement ids the
    sweep's batch-audit row will then collide on.
    """
    event_id = str(uuid.uuid4())
    async with sf() as s:
        row = AuditOutboxRecord(
            event_id=event_id,
            event_type="test.seed",
            occurred_at=occurred_at,
            payload={
                "event_id": event_id,
                "event_type": "test.seed",
                "occurred_at": occurred_at.isoformat(),
                "workspace_id": str(workspace_id),
                "actor": {"type": "system", "id": "test"},
                "data": {},
            },
            delivered_at=occurred_at if delivered else None,
        )
        s.add(row)
        await s.commit()
    return event_id


async def _row_exists(sf: async_sessionmaker[AsyncSession], event_id: str) -> bool:
    async with sf() as s:
        stmt = select(AuditOutboxRecord).where(AuditOutboxRecord.event_id == event_id)
        return (await s.execute(stmt)).scalar_one_or_none() is not None


# ---------------------------------------------------------------------------
# Delta 1: past-cutoff row deletes
# ---------------------------------------------------------------------------


async def test_past_cutoff_row_deletes_within_one_tick(
    sf: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
    workspace_id = await _seed_workspace(sf, name="rotates", audit_retention_days=30)
    # 31 days old → past the 30-day cutoff.
    event_id = await _seed_outbox_row(
        sf, workspace_id=workspace_id, occurred_at=now - timedelta(days=31)
    )

    runner = AuditRetentionSweepRunner(now_fn=lambda: now)
    worker = ScheduleWorker(session_factory=sf, runner=runner, name="audit_retention_sweep_worker")

    deleted = await worker.fire_due_once()

    assert deleted == 1, "the one past-cutoff row should be deleted in this tick"
    assert not await _row_exists(sf, event_id)


# ---------------------------------------------------------------------------
# Delta 2: within-cutoff row survives
# ---------------------------------------------------------------------------


async def test_within_cutoff_row_survives(sf: async_sessionmaker[AsyncSession]) -> None:
    now = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
    workspace_id = await _seed_workspace(sf, name="rotates", audit_retention_days=30)
    # 29 days old → inside the 30-day window.
    event_id = await _seed_outbox_row(
        sf, workspace_id=workspace_id, occurred_at=now - timedelta(days=29)
    )

    runner = AuditRetentionSweepRunner(now_fn=lambda: now)
    worker = ScheduleWorker(session_factory=sf, runner=runner, name="audit_retention_sweep_worker")

    deleted = await worker.fire_due_once()

    assert deleted == 0, "a within-cutoff row must NOT be deleted"
    assert await _row_exists(sf, event_id)


# ---------------------------------------------------------------------------
# Delta 3: NULL-retention workspace is skipped
# ---------------------------------------------------------------------------


async def test_null_retention_workspace_is_skipped(sf: async_sessionmaker[AsyncSession]) -> None:
    """``audit_retention_days = NULL`` means forever — the sweep must not touch the row.

    A workspace with the default (NULL = forever) keeps every row, no
    matter how old. The repository's ``list_with_audit_retention`` filters
    NULL out at the DB level so the sweep never even iterates over it.
    """
    now = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
    workspace_id = await _seed_workspace(sf, name="forever", audit_retention_days=None)
    # An ancient row — 365 days old.
    event_id = await _seed_outbox_row(
        sf, workspace_id=workspace_id, occurred_at=now - timedelta(days=365)
    )

    runner = AuditRetentionSweepRunner(now_fn=lambda: now)
    worker = ScheduleWorker(session_factory=sf, runner=runner, name="audit_retention_sweep_worker")

    deleted = await worker.fire_due_once()

    assert deleted == 0
    assert await _row_exists(sf, event_id)


# ---------------------------------------------------------------------------
# Delta 4: multi-workspace isolation
# ---------------------------------------------------------------------------


async def test_multi_workspace_isolation(sf: async_sessionmaker[AsyncSession]) -> None:
    """Workspace A's retention doesn't reach into workspace B's audit_outbox rows."""
    now = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
    ws_a = await _seed_workspace(sf, name="a", audit_retention_days=7)
    ws_b = await _seed_workspace(sf, name="b", audit_retention_days=None)  # forever
    # A's row is 10 days old (past 7-day cutoff) → must delete.
    a_event = await _seed_outbox_row(sf, workspace_id=ws_a, occurred_at=now - timedelta(days=10))
    # B's row is 10 days old too but B is forever → must survive.
    b_event = await _seed_outbox_row(sf, workspace_id=ws_b, occurred_at=now - timedelta(days=10))

    runner = AuditRetentionSweepRunner(now_fn=lambda: now)
    worker = ScheduleWorker(session_factory=sf, runner=runner, name="audit_retention_sweep_worker")

    deleted = await worker.fire_due_once()
    # 1 from workspace A, plus the sweep-audit-row for A (which DID get
    # written but won't itself be deleted this tick — its occurred_at is
    # ``now``). So the count is exactly 1 (rows DELETED, not emitted).
    assert deleted == 1
    assert not await _row_exists(sf, a_event)
    assert await _row_exists(sf, b_event)


# ---------------------------------------------------------------------------
# Delta 5: glass-box audit row carries trigger + source tags
# ---------------------------------------------------------------------------


async def test_glass_box_audit_row_carries_trigger_and_source(
    sf: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
    workspace_id = await _seed_workspace(sf, name="rotates", audit_retention_days=14)
    await _seed_outbox_row(sf, workspace_id=workspace_id, occurred_at=now - timedelta(days=20))

    runner = AuditRetentionSweepRunner(now_fn=lambda: now)
    worker = ScheduleWorker(session_factory=sf, runner=runner, name="audit_retention_sweep_worker")

    await worker.fire_due_once()

    async with sf() as s:
        stmt = select(AuditOutboxRecord).where(
            AuditOutboxRecord.event_type == AUDIT_RETENTION_SWEPT_EVENT_TYPE
        )
        rows = (await s.execute(stmt)).scalars().all()
    assert len(rows) == 1, "exactly one batch audit row per workspace per non-empty sweep"
    audit_row = rows[0]
    assert audit_row.payload["event_type"] == AUDIT_RETENTION_SWEPT_EVENT_TYPE
    assert audit_row.payload["workspace_id"] == str(workspace_id)
    assert audit_row.payload["data"]["trigger"] == "schedule"
    assert audit_row.payload["data"]["source"] == AUDIT_RETENTION_SOURCE
    assert audit_row.payload["data"]["retention_days"] == 14
    assert audit_row.payload["data"]["deleted_count"] == 1


# ---------------------------------------------------------------------------
# Delta 6: hard delete regardless of delivery state
# ---------------------------------------------------------------------------


async def test_hard_delete_ignores_delivery_state(sf: async_sessionmaker[AsyncSession]) -> None:
    """A row past cutoff is deleted even if ``delivered_at`` is NULL.

    The retention contract is HARD — anything not delivered after N days
    is dead. Keeping un-delivered rows forever would defeat rotation.
    """
    now = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
    workspace_id = await _seed_workspace(sf, name="rotates", audit_retention_days=7)
    undelivered_event = await _seed_outbox_row(
        sf,
        workspace_id=workspace_id,
        occurred_at=now - timedelta(days=10),
        delivered=False,
    )
    delivered_event = await _seed_outbox_row(
        sf,
        workspace_id=workspace_id,
        occurred_at=now - timedelta(days=10),
        delivered=True,
    )

    runner = AuditRetentionSweepRunner(now_fn=lambda: now)
    worker = ScheduleWorker(session_factory=sf, runner=runner, name="audit_retention_sweep_worker")

    deleted = await worker.fire_due_once()
    assert deleted == 2
    assert not await _row_exists(sf, undelivered_event)
    assert not await _row_exists(sf, delivered_event)


# ---------------------------------------------------------------------------
# Empty sweep is a clean no-op (no audit row emitted)
# ---------------------------------------------------------------------------


async def test_empty_sweep_emits_no_audit_row(sf: async_sessionmaker[AsyncSession]) -> None:
    """No workspace has retention → no DELETE + no audit row.

    Keeps the audit log truthful: the sweep ran but did nothing
    observable, so it doesn't pollute the log with a zero-count entry.
    """
    now = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
    await _seed_workspace(sf, name="forever", audit_retention_days=None)

    runner = AuditRetentionSweepRunner(now_fn=lambda: now)
    worker = ScheduleWorker(session_factory=sf, runner=runner, name="audit_retention_sweep_worker")

    deleted = await worker.fire_due_once()
    assert deleted == 0

    async with sf() as s:
        stmt = select(AuditOutboxRecord).where(
            AuditOutboxRecord.event_type == AUDIT_RETENTION_SWEPT_EVENT_TYPE
        )
        assert (await s.execute(stmt)).scalars().first() is None
