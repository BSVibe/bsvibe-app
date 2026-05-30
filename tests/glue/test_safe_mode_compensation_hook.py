"""D3b — auto-compensation on Safe Mode deny/expire transitions.

D3 (PR #215) wired ``backend.delivery.compensation.CompensationHandler`` ONLY
into the ``/deliverables/{id}/retract`` API path (via the plugin
``@p.compensate`` handlers, captured at delivery time). D3a (PR #222) added the
expiry sweep transition + its per-batch audit hook but explicitly LEFT the
auto-compensation wiring to D3b. Today a denied or expired queue row sits with
no compensation evaluation fired — even though the underlying Deliverable may
have been shipped and now needs a supersede/revert/notify decision.

D3b plugs ``CompensationHandler.evaluate(deliverable_id=...)`` into:

* :meth:`SafeModeQueue.deny` — fires the evaluation when ``PENDING → DENIED``
  transitions succeed.
* :class:`SafeModeExpirySweepRunner` — fires the evaluation once per item that
  was successfully flipped to ``EXPIRED`` in the sweep batch.

Direct in-process call (NOT outbox-subscriber-based) because no in-process
audit subscriber framework exists yet (:class:`RelayWorker` drains the outbox
to an external sink, not to local handlers). The "audit hook + subscriber"
seam would need to be invented as part of D3b — that exceeds one-PR scope.

The four deltas asserted here:

1. **Deny fires compensation per item.** A successful ``deny`` invokes
   ``CompensationHandler.evaluate`` exactly once with the Deliverable id from
   the queue row. Today nothing fires.
2. **Expire fires compensation per item.** Each item that the sweep flips to
   ``EXPIRED`` triggers exactly one ``evaluate`` call with that item's
   Deliverable id. The fan-out is per-ITEM (not per-batch) because the
   compensation decision is per-Deliverable.
3. **Idempotence preserved.** A second deny on the same row never re-fires
   (it short-circuits at the lifecycle method via ``False`` return); a second
   expiry sweep tick at the same clock finds nothing past ``expires_at`` so
   it never re-fires either. Compensation invocation count is capped at one
   per row per transition.
4. **Other transitions don't fire.** ``approve`` / ``mark_delivered`` /
   ``archive`` / ``mark_deleted`` / ``extend`` do NOT invoke compensation —
   only the two settled-without-delivery transitions (deny, expire) do.

Bonus: the existing ``/deliverables/{id}/retract`` path (which fires
plugin-level ``@p.compensate`` handlers via ``PluginRetractHandler``, NOT
``CompensationHandler``) is structurally untouched by D3b — the two code
paths address different layers (per-handle plugin revert vs. per-deliverable
supersede/revert/notify decision) and must not collide.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.delivery.db import DeliveryBase, SafeModeQueueItemRow, SafeModeStatus
from backend.delivery.safe_mode_expiry import SafeModeExpirySweepRunner
from backend.delivery.safe_mode_queue import SafeModeQueue
from backend.workers.schedule_runner import ScheduleWorker

from .._support import db_engine

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def sf() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with db_engine(DeliveryBase) as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


async def _enqueue(
    sf_: async_sessionmaker[AsyncSession],
    *,
    workspace_id: uuid.UUID,
    deliverable_id: uuid.UUID | None = None,
    expires_at: datetime | None = None,
    status: SafeModeStatus = SafeModeStatus.PENDING,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Seed one queue row; returns ``(item_id, deliverable_id)``."""
    item_id = uuid.uuid4()
    deliv_id = deliverable_id or uuid.uuid4()
    exp = expires_at or (datetime.now(tz=UTC) + timedelta(days=90))
    async with sf_() as s:
        s.add(
            SafeModeQueueItemRow(
                id=item_id,
                workspace_id=workspace_id,
                deliverable_id=deliv_id,
                run_id=None,
                status=status,
                expires_at=exp,
                extension_count=0,
            )
        )
        await s.commit()
    return item_id, deliv_id


# ---------------------------------------------------------------------------
# Delta 1: Deny fires compensation per item
# ---------------------------------------------------------------------------


async def test_deny_fires_compensation_once_with_deliverable_id(
    sf: async_sessionmaker[AsyncSession],
) -> None:
    """A successful ``SafeModeQueue.deny`` (PENDING → DENIED) must invoke
    ``CompensationHandler.evaluate`` exactly once with the row's
    ``deliverable_id``. Today nothing fires — D3b adds this wiring."""
    workspace_id = uuid.uuid4()
    item_id, deliverable_id = await _enqueue(sf, workspace_id=workspace_id)

    with patch("backend.delivery.safe_mode_compensation_hook.CompensationHandler") as mock_cls:
        instance = mock_cls.return_value
        instance.evaluate = AsyncMock(return_value=None)

        async with sf() as s:
            q = SafeModeQueue(s)
            ok = await q.deny(
                workspace_id=workspace_id,
                item_id=item_id,
                actor_id=uuid.uuid4(),
                reason="not relevant",
            )
            await s.commit()

        assert ok is True
        assert instance.evaluate.await_count == 1
        kwargs = instance.evaluate.await_args.kwargs
        assert kwargs.get("deliverable_id") == deliverable_id


async def test_deny_failed_transition_does_not_fire_compensation(
    sf: async_sessionmaker[AsyncSession],
) -> None:
    """A ``deny`` that fails (already-decided row, wrong workspace) MUST NOT
    fire compensation — only the actual lifecycle flip triggers the hook."""
    workspace_id = uuid.uuid4()
    item_id, _ = await _enqueue(sf, workspace_id=workspace_id, status=SafeModeStatus.APPROVED)

    with patch("backend.delivery.safe_mode_compensation_hook.CompensationHandler") as mock_cls:
        instance = mock_cls.return_value
        instance.evaluate = AsyncMock(return_value=None)

        async with sf() as s:
            q = SafeModeQueue(s)
            ok = await q.deny(
                workspace_id=workspace_id,
                item_id=item_id,
                actor_id=uuid.uuid4(),
                reason="cannot deny approved row",
            )
            await s.commit()

        # The lifecycle method rejected the transition (PENDING-only edge).
        assert ok is False
        assert instance.evaluate.await_count == 0


async def test_double_deny_only_fires_compensation_once(
    sf: async_sessionmaker[AsyncSession],
) -> None:
    """Second deny on the same row is a no-op at the lifecycle method
    (returns False because the row is no longer PENDING). Compensation must
    not be re-fired by the second call."""
    workspace_id = uuid.uuid4()
    item_id, deliverable_id = await _enqueue(sf, workspace_id=workspace_id)

    with patch("backend.delivery.safe_mode_compensation_hook.CompensationHandler") as mock_cls:
        instance = mock_cls.return_value
        instance.evaluate = AsyncMock(return_value=None)

        async with sf() as s:
            q = SafeModeQueue(s)
            first = await q.deny(
                workspace_id=workspace_id,
                item_id=item_id,
                actor_id=uuid.uuid4(),
                reason="r1",
            )
            second = await q.deny(
                workspace_id=workspace_id,
                item_id=item_id,
                actor_id=uuid.uuid4(),
                reason="r2",
            )
            await s.commit()

        assert first is True
        assert second is False  # already DENIED
        assert instance.evaluate.await_count == 1
        kwargs = instance.evaluate.await_args.kwargs
        assert kwargs.get("deliverable_id") == deliverable_id


# ---------------------------------------------------------------------------
# Delta 2: Expire sweep fires compensation per expired item
# ---------------------------------------------------------------------------


async def test_expire_sweep_fires_compensation_per_item(
    sf: async_sessionmaker[AsyncSession],
) -> None:
    """The sweep that flips N items to EXPIRED in one batch must fire
    ``CompensationHandler.evaluate`` exactly N times — once per item, with
    each item's Deliverable id. The per-batch audit row (D3a) is one
    operational event; the compensation fan-out is per-item because the
    decision is per-Deliverable."""
    workspace_id = uuid.uuid4()
    now = datetime(2026, 5, 25, 12, 0, 0, tzinfo=UTC)
    past = now - timedelta(seconds=1)

    item_a_id, deliv_a = await _enqueue(sf, workspace_id=workspace_id, expires_at=past)
    item_b_id, deliv_b = await _enqueue(sf, workspace_id=workspace_id, expires_at=past)

    with patch("backend.delivery.safe_mode_compensation_hook.CompensationHandler") as mock_cls:
        instance = mock_cls.return_value
        instance.evaluate = AsyncMock(return_value=None)

        runner = SafeModeExpirySweepRunner(now_fn=lambda: now)
        worker = ScheduleWorker(session_factory=sf, runner=runner, name="safe_mode_expiry_worker")
        expired = await worker.fire_due_once()

        assert expired == 2
        assert instance.evaluate.await_count == 2
        called_deliverable_ids = {
            call.kwargs.get("deliverable_id") for call in instance.evaluate.await_args_list
        }
        assert called_deliverable_ids == {deliv_a, deliv_b}
    # Use the IDs to silence "unused" warnings — they're checked above.
    del item_a_id, item_b_id


async def test_expire_sweep_no_expired_does_not_fire(
    sf: async_sessionmaker[AsyncSession],
) -> None:
    """If the sweep finds nothing past expires_at, compensation must not
    fire (matches D3a's "no audit emitted on empty batch" pattern)."""
    workspace_id = uuid.uuid4()
    now = datetime(2026, 5, 25, 12, 0, 0, tzinfo=UTC)
    await _enqueue(sf, workspace_id=workspace_id, expires_at=now + timedelta(hours=1))

    with patch("backend.delivery.safe_mode_compensation_hook.CompensationHandler") as mock_cls:
        instance = mock_cls.return_value
        instance.evaluate = AsyncMock(return_value=None)

        runner = SafeModeExpirySweepRunner(now_fn=lambda: now)
        worker = ScheduleWorker(session_factory=sf, runner=runner, name="safe_mode_expiry_worker")
        fired = await worker.fire_due_once()

        assert fired == 0
        assert instance.evaluate.await_count == 0


# ---------------------------------------------------------------------------
# Delta 3: Idempotence — second sweep tick doesn't re-fire compensation
# ---------------------------------------------------------------------------


async def test_second_sweep_tick_does_not_re_fire_compensation(
    sf: async_sessionmaker[AsyncSession],
) -> None:
    """Two sweep ticks at the same clock: the first flips the row and
    fires compensation; the second finds nothing PENDING/EXTENDED past
    expires_at and must NOT call compensation again."""
    workspace_id = uuid.uuid4()
    now = datetime(2026, 5, 25, 12, 0, 0, tzinfo=UTC)
    await _enqueue(sf, workspace_id=workspace_id, expires_at=now - timedelta(seconds=1))

    with patch("backend.delivery.safe_mode_compensation_hook.CompensationHandler") as mock_cls:
        instance = mock_cls.return_value
        instance.evaluate = AsyncMock(return_value=None)

        runner = SafeModeExpirySweepRunner(now_fn=lambda: now)
        worker = ScheduleWorker(session_factory=sf, runner=runner, name="safe_mode_expiry_worker")
        first = await worker.fire_due_once()
        second = await worker.fire_due_once()

        assert first == 1
        assert second == 0
        assert instance.evaluate.await_count == 1


# ---------------------------------------------------------------------------
# Delta 4: Other transitions don't fire compensation
# ---------------------------------------------------------------------------


async def test_approve_does_not_fire_compensation(
    sf: async_sessionmaker[AsyncSession],
) -> None:
    """Approve is a positive transition — the delivery is about to be
    dispatched, NOT compensated for. The hook must not fire."""
    workspace_id = uuid.uuid4()
    item_id, _ = await _enqueue(sf, workspace_id=workspace_id)

    with patch("backend.delivery.safe_mode_compensation_hook.CompensationHandler") as mock_cls:
        instance = mock_cls.return_value
        instance.evaluate = AsyncMock(return_value=None)

        async with sf() as s:
            q = SafeModeQueue(s)
            ok = await q.approve(
                workspace_id=workspace_id,
                item_id=item_id,
                actor_id=uuid.uuid4(),
            )
            await s.commit()

        assert ok is True
        assert instance.evaluate.await_count == 0


async def test_mark_delivered_does_not_fire_compensation(
    sf: async_sessionmaker[AsyncSession],
) -> None:
    """``mark_delivered`` is the success terminal — compensation is the
    rollback path. The hook must not fire on delivery completion."""
    workspace_id = uuid.uuid4()
    item_id, _ = await _enqueue(sf, workspace_id=workspace_id, status=SafeModeStatus.APPROVED)

    with patch("backend.delivery.safe_mode_compensation_hook.CompensationHandler") as mock_cls:
        instance = mock_cls.return_value
        instance.evaluate = AsyncMock(return_value=None)

        async with sf() as s:
            q = SafeModeQueue(s)
            ok = await q.mark_delivered(workspace_id=workspace_id, item_id=item_id)
            await s.commit()

        assert ok is True
        assert instance.evaluate.await_count == 0


async def test_archive_does_not_fire_compensation(
    sf: async_sessionmaker[AsyncSession],
) -> None:
    """Archive is housekeeping — moves a settled item out of the active
    queue without changing what was delivered. The hook must not fire."""
    workspace_id = uuid.uuid4()
    item_id, _ = await _enqueue(sf, workspace_id=workspace_id, status=SafeModeStatus.DELIVERED)

    with patch("backend.delivery.safe_mode_compensation_hook.CompensationHandler") as mock_cls:
        instance = mock_cls.return_value
        instance.evaluate = AsyncMock(return_value=None)

        async with sf() as s:
            q = SafeModeQueue(s)
            ok = await q.archive(workspace_id=workspace_id, item_id=item_id)
            await s.commit()

        assert ok is True
        assert instance.evaluate.await_count == 0


async def test_mark_deleted_does_not_fire_compensation(
    sf: async_sessionmaker[AsyncSession],
) -> None:
    """``mark_deleted`` is the retention tombstone — the row is being
    GC'd, NOT compensated for."""
    workspace_id = uuid.uuid4()
    item_id, _ = await _enqueue(sf, workspace_id=workspace_id, status=SafeModeStatus.ARCHIVED)

    with patch("backend.delivery.safe_mode_compensation_hook.CompensationHandler") as mock_cls:
        instance = mock_cls.return_value
        instance.evaluate = AsyncMock(return_value=None)

        async with sf() as s:
            q = SafeModeQueue(s)
            ok = await q.mark_deleted(workspace_id=workspace_id, item_id=item_id)
            await s.commit()

        assert ok is True
        assert instance.evaluate.await_count == 0


async def test_extend_does_not_fire_compensation(
    sf: async_sessionmaker[AsyncSession],
) -> None:
    """Extending the window is the opposite of a settled-without-delivery
    outcome — the item stays in PENDING/EXTENDED, no compensation."""
    workspace_id = uuid.uuid4()
    item_id, _ = await _enqueue(sf, workspace_id=workspace_id)

    with patch("backend.delivery.safe_mode_compensation_hook.CompensationHandler") as mock_cls:
        instance = mock_cls.return_value
        instance.evaluate = AsyncMock(return_value=None)

        async with sf() as s:
            q = SafeModeQueue(s)
            ok = await q.extend(workspace_id=workspace_id, item_id=item_id)
            await s.commit()

        assert ok is True
        assert instance.evaluate.await_count == 0


# ---------------------------------------------------------------------------
# Hook resilience — a compensation failure must not break the lifecycle flip
# ---------------------------------------------------------------------------


async def test_deny_succeeds_even_when_compensation_evaluate_raises(
    sf: async_sessionmaker[AsyncSession],
) -> None:
    """A flaky compensation evaluator (downstream DB hiccup, missing
    deliverable row, etc.) must NOT prevent the queue row from settling.
    The lifecycle flip already happened; the hook is best-effort."""
    workspace_id = uuid.uuid4()
    item_id, _ = await _enqueue(sf, workspace_id=workspace_id)

    with patch("backend.delivery.safe_mode_compensation_hook.CompensationHandler") as mock_cls:
        instance = mock_cls.return_value
        instance.evaluate = AsyncMock(side_effect=RuntimeError("downstream blip"))

        async with sf() as s:
            q = SafeModeQueue(s)
            ok = await q.deny(
                workspace_id=workspace_id,
                item_id=item_id,
                actor_id=uuid.uuid4(),
                reason="r",
            )
            await s.commit()

        assert ok is True
        assert instance.evaluate.await_count == 1

    async with sf() as s:
        row = await s.get(SafeModeQueueItemRow, item_id)
    assert row is not None
    assert row.status is SafeModeStatus.DENIED


async def test_expire_sweep_continues_when_one_compensation_evaluate_raises(
    sf: async_sessionmaker[AsyncSession],
) -> None:
    """One item's compensation evaluator raising must NOT abort the batch —
    every successfully expired row still flips, and compensation fires for
    every item the hook reached. Per-item soft-fail mirrors D3a's per-row
    transition guard."""
    workspace_id = uuid.uuid4()
    now = datetime(2026, 5, 25, 12, 0, 0, tzinfo=UTC)
    past = now - timedelta(seconds=1)

    await _enqueue(sf, workspace_id=workspace_id, expires_at=past)
    await _enqueue(sf, workspace_id=workspace_id, expires_at=past)

    with patch("backend.delivery.safe_mode_compensation_hook.CompensationHandler") as mock_cls:
        instance = mock_cls.return_value
        # First call raises, second succeeds.
        instance.evaluate = AsyncMock(side_effect=[RuntimeError("first compensation hiccup"), None])

        runner = SafeModeExpirySweepRunner(now_fn=lambda: now)
        worker = ScheduleWorker(session_factory=sf, runner=runner, name="safe_mode_expiry_worker")
        expired = await worker.fire_due_once()

        # Both rows flipped (the lifecycle transitions are independent of the
        # hook), and the hook tried both items.
        assert expired == 2
        assert instance.evaluate.await_count == 2
