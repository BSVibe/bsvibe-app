"""ScheduleWorker — DB-poll schedule runner end-to-end (M1).

Proves the M1 lift's four deltas attributable to the new runner:

1. **End-to-end fire.** A ``workspace_schedules`` row with
   ``next_run_at <= now`` results in **exactly one** spawned run within
   one polling tick — neither zero (the emitter wasn't called) nor two
   (claim semantics drifted).
2. **Idempotence.** A second tick **in the same window** (before the
   advancer has moved ``next_run_at`` forward — or under a paused
   advancer that holds it steady) **never** spawns a second
   ``TriggerEventRow`` for the same emitter row in that window. This
   asserts at the **spawn site** (the ``TriggerEventRow`` unique
   constraint), not just a worker log.
3. **Audit / glass-box.** The spawned trigger carries
   ``payload["trigger"] == "schedule"`` so the Brief/Run views can show
   "schedule-triggered." The downstream Receive→Request mint propagates
   the payload so the ``RequestRow`` also reflects it.
4. **Swappable seam.** ``ScheduleWorker`` depends on a
   ``ScheduleRunnerProtocol`` — substituting a no-op impl makes the
   worker tick a no-op, proving the worker doesn't reach past the seam.

The tests run on real Postgres when ``BSVIBE_DATABASE_URL`` is set +
reachable (mirrors the other glue suites), so the FK-bound migration is
validated against PG, not just the SQLite test tier.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.intake.db import IntakeBase, RequestRow, TriggerEventRow, TriggerKind
from backend.intake.schedule import ScheduleTrigger
from backend.intake.schedule_db import WorkspaceScheduleRow
from backend.workers.intake_worker import IntakeWorker
from backend.workers.schedule_runner import (
    DbPollScheduleRunner,
    ScheduleRunnerProtocol,
    ScheduleWorker,
)

from .._support import db_engine

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def sf() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with db_engine(IntakeBase) as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


class _FixedAdvancer:
    """Test advancer that holds ``next_run_at`` steady (the 'same window').

    Useful for asserting idempotence: a runner tick must NOT spawn a
    second TriggerEvent for an emitter row whose ``next_run_at`` it has
    not yet moved past. Reproduces a clock that hasn't ticked / a cron
    expression that hasn't matured to its next firing yet.
    """

    def __init__(self, *, hold_at: datetime) -> None:
        self._hold_at = hold_at

    def next_after(self, *, cron_expr: str, after: datetime) -> datetime:
        # Hold steady — this is the "still in the same window" case.
        return self._hold_at


class _FixedIntervalAdvancer:
    """Test advancer that advances by a fixed ``timedelta``.

    Proves the runner's spawn-and-advance pairing under a clock that
    moves: one tick spawns one trigger and bumps ``next_run_at`` forward
    by the interval, so a follow-up tick (still before the new
    ``next_run_at``) is a no-op.
    """

    def __init__(self, *, interval: timedelta) -> None:
        self._interval = interval

    def next_after(self, *, cron_expr: str, after: datetime) -> datetime:
        return after + self._interval


async def _add_schedule(
    sf: async_sessionmaker[AsyncSession],
    *,
    workspace_id: uuid.UUID,
    plugin_name: str,
    next_run_at: datetime,
    enabled: bool = True,
) -> uuid.UUID:
    sched_id = uuid.uuid4()
    async with sf() as s:
        s.add(
            WorkspaceScheduleRow(
                id=sched_id,
                workspace_id=workspace_id,
                product_id=None,
                plugin_name=plugin_name,
                cron_expr="*/5 * * * *",
                next_run_at=next_run_at,
                last_fired_at=None,
                enabled=enabled,
            )
        )
        await s.commit()
    return sched_id


# ---------------------------------------------------------------------------
# Delta 1: end-to-end fire — exactly one spawn within the polling tick
# ---------------------------------------------------------------------------


async def test_due_schedule_spawns_exactly_one_run(
    sf: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id = uuid.uuid4()
    now = datetime(2026, 5, 25, 12, 0, 0, tzinfo=UTC)
    await _add_schedule(
        sf,
        workspace_id=workspace_id,
        plugin_name="weekly-summary",
        next_run_at=now - timedelta(seconds=1),  # due
    )

    worker = ScheduleWorker(
        session_factory=sf,
        runner=DbPollScheduleRunner(
            advancer=_FixedIntervalAdvancer(interval=timedelta(minutes=5)),
            now_fn=lambda: now,
        ),
    )

    fired = await worker.fire_due_once()

    assert fired == 1, "exactly one schedule row should fire when one is due"

    # Spawn-site assertion (not worker log): one TriggerEventRow exists for
    # this workspace, with trigger_kind=schedule.
    async with sf() as s:
        rows = (
            (
                await s.execute(
                    select(TriggerEventRow).where(TriggerEventRow.workspace_id == workspace_id)
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1
    assert rows[0].trigger_kind == TriggerKind.SCHEDULE


# ---------------------------------------------------------------------------
# Delta 2: idempotence — same window never double-spawns
# ---------------------------------------------------------------------------


async def test_same_window_tick_does_not_double_spawn(
    sf: async_sessionmaker[AsyncSession],
) -> None:
    """Two ticks pointing at the SAME ``fired_at`` must collapse to ONE
    TriggerEventRow.

    The advancer is held steady (clock paused inside one window). The
    runner must not spawn a duplicate — the contract is asserted at the
    spawn site (the TriggerEventRow unique constraint on
    ``(workspace_id, source, idempotency_key)``).
    """
    workspace_id = uuid.uuid4()
    now = datetime(2026, 5, 25, 12, 0, 0, tzinfo=UTC)
    await _add_schedule(
        sf,
        workspace_id=workspace_id,
        plugin_name="cron-plugin",
        next_run_at=now,
    )

    worker = ScheduleWorker(
        session_factory=sf,
        runner=DbPollScheduleRunner(
            advancer=_FixedAdvancer(hold_at=now),
            now_fn=lambda: now + timedelta(seconds=1),
        ),
    )

    # Two ticks in the same paused window. The second must NOT mint a
    # second TriggerEventRow even if the row still looks 'due'.
    await worker.fire_due_once()
    await worker.fire_due_once()

    async with sf() as s:
        rows = (
            (
                await s.execute(
                    select(TriggerEventRow).where(TriggerEventRow.workspace_id == workspace_id)
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1, "same window must collapse to a single trigger event"


async def test_advanced_window_fires_a_second_run(
    sf: async_sessionmaker[AsyncSession],
) -> None:
    """A NEW window (advancer moved next_run_at forward AND clock crossed
    the new threshold) IS allowed to fire — idempotence is per-window, not
    permanent. Complements the 'same window' delta so the assertion isn't
    a no-op runner masquerading as idempotent."""
    workspace_id = uuid.uuid4()
    t0 = datetime(2026, 5, 25, 12, 0, 0, tzinfo=UTC)
    await _add_schedule(
        sf,
        workspace_id=workspace_id,
        plugin_name="every-5-min",
        next_run_at=t0,
    )

    # Use a real-interval advancer so each fire moves next_run_at forward.
    advancer = _FixedIntervalAdvancer(interval=timedelta(minutes=5))

    # First tick at t0 — fires the t0 window, advances to t0+5min.
    worker_t0 = ScheduleWorker(
        session_factory=sf,
        runner=DbPollScheduleRunner(advancer=advancer, now_fn=lambda: t0),
    )
    await worker_t0.fire_due_once()

    # Second tick at t0+6min — the new next_run_at (t0+5min) is now due
    # again, so this fires the SECOND, distinct window.
    t1 = t0 + timedelta(minutes=6)
    worker_t1 = ScheduleWorker(
        session_factory=sf,
        runner=DbPollScheduleRunner(advancer=advancer, now_fn=lambda: t1),
    )
    await worker_t1.fire_due_once()

    async with sf() as s:
        rows = (
            (
                await s.execute(
                    select(TriggerEventRow).where(TriggerEventRow.workspace_id == workspace_id)
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 2, "two distinct windows must produce two trigger events"


# ---------------------------------------------------------------------------
# Delta 3: audit / glass-box — trigger=schedule visible on the payload
# ---------------------------------------------------------------------------


async def test_spawned_trigger_carries_schedule_audit_tag(
    sf: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id = uuid.uuid4()
    now = datetime(2026, 5, 25, 12, 0, 0, tzinfo=UTC)
    await _add_schedule(
        sf,
        workspace_id=workspace_id,
        plugin_name="audit-tagged",
        next_run_at=now,
    )

    worker = ScheduleWorker(
        session_factory=sf,
        runner=DbPollScheduleRunner(
            advancer=_FixedIntervalAdvancer(interval=timedelta(minutes=5)),
            now_fn=lambda: now,
        ),
    )
    await worker.fire_due_once()

    async with sf() as s:
        trig = (
            (
                await s.execute(
                    select(TriggerEventRow).where(TriggerEventRow.workspace_id == workspace_id)
                )
            )
            .scalars()
            .one()
        )

    # Glass-box delta on the TriggerEventRow: payload carries the
    # schedule audit tag so Brief/Run views can render "schedule-triggered."
    assert trig.payload.get("trigger") == "schedule"
    # And the original cron metadata is preserved (provenance).
    assert trig.payload.get("plugin") == "audit-tagged"


async def test_request_payload_propagates_schedule_audit_tag(
    sf: async_sessionmaker[AsyncSession],
) -> None:
    """The downstream IntakeWorker drains the TriggerEvent into a
    Request; that Request's payload must surface the same
    ``trigger=schedule`` tag so a founder reading the Brief/Run view can
    tell where the run came from."""
    workspace_id = uuid.uuid4()
    now = datetime(2026, 5, 25, 12, 0, 0, tzinfo=UTC)
    await _add_schedule(
        sf,
        workspace_id=workspace_id,
        plugin_name="downstream-tag",
        next_run_at=now,
    )

    schedule_worker = ScheduleWorker(
        session_factory=sf,
        runner=DbPollScheduleRunner(
            advancer=_FixedIntervalAdvancer(interval=timedelta(minutes=5)),
            now_fn=lambda: now,
        ),
    )
    await schedule_worker.fire_due_once()

    intake = IntakeWorker(session_factory=sf)
    await intake.drain_once()

    async with sf() as s:
        req = (
            (await s.execute(select(RequestRow).where(RequestRow.workspace_id == workspace_id)))
            .scalars()
            .one()
        )
    assert req.payload.get("trigger") == "schedule"


# ---------------------------------------------------------------------------
# Delta 4: swappable seam — ScheduleWorker depends on a Protocol
# ---------------------------------------------------------------------------


class _NoopScheduleRunner:
    """A no-op runner — proves ScheduleWorker calls the seam exactly once
    per tick AND nothing beyond the seam is reached when the impl returns
    zero fired rows (no TriggerEventRow, no DB mutation past the row
    fetch)."""

    def __init__(self) -> None:
        self.calls = 0

    async def fire_due(
        self, *, session_factory: async_sessionmaker[AsyncSession], now: datetime
    ) -> int:
        self.calls += 1
        return 0


async def test_worker_depends_on_protocol_not_concrete_impl(
    sf: async_sessionmaker[AsyncSession],
) -> None:
    """Substitute a no-op runner via the Protocol — the worker tick must
    delegate to it without reaching past it. Type-checked via the
    Protocol surface (``mypy --strict`` catches a contract drift)."""
    workspace_id = uuid.uuid4()
    now = datetime(2026, 5, 25, 12, 0, 0, tzinfo=UTC)
    await _add_schedule(
        sf,
        workspace_id=workspace_id,
        plugin_name="noop-runner-target",
        next_run_at=now - timedelta(minutes=1),
    )

    noop: ScheduleRunnerProtocol = _NoopScheduleRunner()
    worker = ScheduleWorker(session_factory=sf, runner=noop)

    fired = await worker.fire_due_once()

    assert fired == 0
    assert noop.calls == 1  # type: ignore[attr-defined]  # noop test impl carries a counter

    # And no TriggerEventRow was created — the worker stopped at the seam.
    async with sf() as s:
        rows = (
            (
                await s.execute(
                    select(TriggerEventRow).where(TriggerEventRow.workspace_id == workspace_id)
                )
            )
            .scalars()
            .all()
        )
    assert rows == []


# ---------------------------------------------------------------------------
# Disabled schedules + future-due schedules must NOT fire
# ---------------------------------------------------------------------------


async def test_disabled_schedule_does_not_fire(
    sf: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id = uuid.uuid4()
    now = datetime(2026, 5, 25, 12, 0, 0, tzinfo=UTC)
    await _add_schedule(
        sf,
        workspace_id=workspace_id,
        plugin_name="disabled-one",
        next_run_at=now - timedelta(minutes=1),
        enabled=False,
    )

    worker = ScheduleWorker(
        session_factory=sf,
        runner=DbPollScheduleRunner(
            advancer=_FixedIntervalAdvancer(interval=timedelta(minutes=5)),
            now_fn=lambda: now,
        ),
    )
    fired = await worker.fire_due_once()
    assert fired == 0


async def test_future_schedule_does_not_fire(
    sf: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id = uuid.uuid4()
    now = datetime(2026, 5, 25, 12, 0, 0, tzinfo=UTC)
    await _add_schedule(
        sf,
        workspace_id=workspace_id,
        plugin_name="future-one",
        next_run_at=now + timedelta(minutes=10),
    )

    worker = ScheduleWorker(
        session_factory=sf,
        runner=DbPollScheduleRunner(
            advancer=_FixedIntervalAdvancer(interval=timedelta(minutes=5)),
            now_fn=lambda: now,
        ),
    )
    fired = await worker.fire_due_once()
    assert fired == 0


# ---------------------------------------------------------------------------
# Bonus — ScheduleTrigger itself stamps the audit tag (so any caller, not
# just the worker, gets the trigger=schedule audit field)
# ---------------------------------------------------------------------------


async def test_schedule_trigger_stamps_audit_tag_in_payload(
    sf: async_sessionmaker[AsyncSession],
) -> None:
    """A direct call to ``ScheduleTrigger.fire()`` (not via the worker)
    must also produce a payload with ``trigger=schedule``. Locks the
    audit tag at the emitter site so future runner variants can't drop
    it."""
    workspace_id = uuid.uuid4()
    now = datetime(2026, 5, 25, 12, 0, 0, tzinfo=UTC)
    async with sf() as s:
        trig = ScheduleTrigger(s)
        outcome = await trig.fire(
            workspace_id=workspace_id,
            plugin_name="direct-call",
            cron_expr="*/5 * * * *",
            fired_at=now,
        )
        await s.commit()
    assert outcome.event.payload.get("trigger") == "schedule"
