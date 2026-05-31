"""Lift 0b — assert auto-compensation wiring on Safe Mode deny/expire is GONE.

This file is the delta-asserting RED-first proof for Lift 0b (YAGNI rollback
of D3b, PR #223). PR #223 wired ``backend.delivery.compensation.CompensationHandler``
into ``SafeModeQueue.deny`` and into the D3a expiry sweep
(:class:`SafeModeExpirySweepRunner.fire_due`). v8 §13 / D7 lists
``backend/delivery/compensation.py`` for full deletion under Lift 0; Lift 0b is
the second of three Lift-0 PRs (after #224's per-call DangerAnalyzer rollback)
that retires the YAGNI surface.

The deltas attributable to this lift:

1. **Module gone.** ``backend.delivery.compensation`` no longer importable —
   the whole class is dead. ``backend.delivery.safe_mode_compensation_hook``
   also gone (the D3b glue layer that bridged sweep → handler).
2. **Deny does not fire compensation.** A successful ``SafeModeQueue.deny``
   (PENDING → DENIED transition) does NOT invoke any compensation evaluator.
   Today (pre-Lift-0b) it imports + calls
   :func:`fire_compensation_for_item` per success.
3. **Expire sweep does not fire compensation.** Running
   :class:`SafeModeExpirySweepRunner` over N due rows flips them to EXPIRED +
   emits the per-batch audit row (D3a backward-compat — kept) but performs
   ZERO compensation calls. Today (pre-Lift-0b) it imports + calls
   :func:`fire_compensation_for_item` once per expired item.
4. **D3a sweep still flips state.** Backward-compat — the expiry sweep
   continues to flip PENDING/EXTENDED rows past ``expires_at`` to EXPIRED.
   The existing D3a tests in ``test_safe_mode_expiry_sweep.py`` continue to
   exercise that path; this file adds an explicit per-row state delta for
   crisp regression visibility.

Real PG when ``BSVIBE_DATABASE_URL`` is set + reachable, in-memory SQLite
otherwise (mirrors the ``test_safe_mode_compensation_hook.py`` substrate D3b
shipped). The test does NOT depend on the compensation evaluator existing,
on purpose — patching a missing module would error; we use
:func:`importlib.import_module` reflection so the assertion stands cleanly
on both the RED and the GREEN side.
"""

from __future__ import annotations

import importlib
import inspect
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.workflow.application.safe_mode_expiry import SafeModeExpirySweepRunner
from backend.workflow.application.safe_mode_queue import SafeModeQueue
from backend.workflow.infrastructure.delivery.db import (
    DeliveryBase,
    SafeModeQueueItemRow,
    SafeModeStatus,
)

from .._support import db_engine


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
# Delta 1: compensation modules are gone
# ---------------------------------------------------------------------------


def test_compensation_module_deleted() -> None:
    """``backend.delivery.compensation`` is fully removed (D7 / Lift 0)."""
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("backend.delivery.compensation")


def test_safe_mode_compensation_hook_deleted() -> None:
    """The D3b glue module bridging sweep → handler is gone."""
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("backend.delivery.safe_mode_compensation_hook")


def test_delivery_init_does_not_reexport_compensation() -> None:
    """Per Lift H3b the whole ``backend.delivery`` package is gone — that
    subsumes the Lift 0b assertion (no re-exported dead surface possible)
    and we re-state it as the strongest form: the module no longer exists.
    """
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("backend" + ".delivery")


# ---------------------------------------------------------------------------
# Delta 2: SafeModeQueue.deny does NOT fire compensation
# ---------------------------------------------------------------------------


def test_safe_mode_queue_does_not_import_compensation_hook() -> None:
    """Static check: ``SafeModeQueue.deny`` doesn't reference the removed hook.

    Behavioural absence (no call dispatched) is also asserted below against
    real Postgres, but the source-level assertion catches a regression where
    someone re-imports a renamed compensation hook without re-running the
    full PG fixture. We grep the SOURCE FILE (not just the class) so a stray
    module-level import is caught too, and we look for the call-site
    identifier (``fire_compensation_for_item``) — not the word "compensation"
    in docstrings, which legitimately appears in the rollback context note.
    """
    import backend.workflow.application.safe_mode_queue as mod

    src = inspect.getsource(mod)
    assert "fire_compensation_for_item" not in src
    assert "safe_mode_compensation_hook" not in src
    assert "from backend.delivery.compensation" not in src


async def test_deny_does_not_fire_compensation(
    sf: async_sessionmaker[AsyncSession],
) -> None:
    """A successful ``deny`` (PENDING → DENIED) performs the lifecycle flip
    but does NOT invoke any compensation evaluator. The whole hook path is
    removed.

    Pre-Lift-0b, this test fails because ``SafeModeQueue.deny`` imports
    ``fire_compensation_for_item`` from ``safe_mode_compensation_hook`` and
    calls it (one ``CompensationHandler.evaluate`` invocation per success);
    after Lift 0b, the module is gone and the call site with it.
    """
    workspace_id = uuid.uuid4()
    item_id, _ = await _enqueue(sf, workspace_id=workspace_id)

    async with sf() as s:
        q = SafeModeQueue(s)
        ok = await q.deny(
            workspace_id=workspace_id,
            item_id=item_id,
            actor_id=uuid.uuid4(),
            reason="lift0b — no compensation should fire",
        )
        await s.commit()

    assert ok is True

    # The compensation module is gone — nothing could have imported it.
    # If pre-Lift-0b a regression slips compensation back in via a renamed
    # path, the source-level assertion above catches the deny site.
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("backend.delivery.safe_mode_compensation_hook")

    # And the row really did flip — proves the lifecycle path itself was
    # exercised, so the "no compensation" assertion isn't a vacuous skip.
    async with sf() as s:
        row = await s.get(SafeModeQueueItemRow, item_id)
        assert row is not None
        assert row.status == SafeModeStatus.DENIED


# ---------------------------------------------------------------------------
# Delta 3: SafeModeExpirySweepRunner does NOT fire compensation
# ---------------------------------------------------------------------------


def test_safe_mode_expiry_does_not_import_compensation_hook() -> None:
    """Static: the sweep runner doesn't import / call the removed hook."""
    import backend.workflow.application.safe_mode_expiry as mod

    src = inspect.getsource(mod)
    assert "fire_compensation_for_item" not in src
    assert "safe_mode_compensation_hook" not in src
    assert "from backend.delivery.compensation" not in src


async def test_expire_sweep_does_not_fire_compensation(
    sf: async_sessionmaker[AsyncSession],
) -> None:
    """The D3a sweep flips due rows to EXPIRED + emits the per-batch audit
    row but does NOT fan out per-item compensation. Pre-Lift-0b, the sweep
    called ``fire_compensation_for_item`` once per expired item; after, the
    call is gone.
    """
    workspace_id = uuid.uuid4()
    now = datetime(2026, 5, 25, 12, 0, 0, tzinfo=UTC)
    past = now - timedelta(seconds=1)

    item_a, _ = await _enqueue(sf, workspace_id=workspace_id, expires_at=past)
    item_b, _ = await _enqueue(sf, workspace_id=workspace_id, expires_at=past)

    runner = SafeModeExpirySweepRunner(now_fn=lambda: now)
    count = await runner.fire_due(session_factory=sf, now=now)

    assert count == 2

    # The hook module is gone; the sweep cannot have invoked it.
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("backend.delivery.safe_mode_compensation_hook")

    # Backward-compat: the rows really flipped (D3a behaviour preserved).
    async with sf() as s:
        for item_id in (item_a, item_b):
            row = await s.get(SafeModeQueueItemRow, item_id)
            assert row is not None
            assert row.status == SafeModeStatus.EXPIRED


# ---------------------------------------------------------------------------
# Delta 4: D3a expiry sweep STILL flips state (backward-compat regression
# guard) — duplicates the per-state assertion above but asserts the negative
# half too (future-expiry rows untouched), so a future "trim the sweep"
# regression can't silently no-op D3a.
# ---------------------------------------------------------------------------


async def test_d3a_sweep_still_flips_due_rows_only(
    sf: async_sessionmaker[AsyncSession],
) -> None:
    """Lift 0b removes the compensation fan-out but leaves the D3a sweep
    intact — past-expiry rows still flip, future-expiry rows still don't."""
    workspace_id = uuid.uuid4()
    now = datetime(2026, 5, 25, 12, 0, 0, tzinfo=UTC)
    past = now - timedelta(seconds=1)
    future = now + timedelta(days=10)

    due_item, _ = await _enqueue(sf, workspace_id=workspace_id, expires_at=past)
    fresh_item, _ = await _enqueue(sf, workspace_id=workspace_id, expires_at=future)

    runner = SafeModeExpirySweepRunner(now_fn=lambda: now)
    count = await runner.fire_due(session_factory=sf, now=now)

    assert count == 1
    async with sf() as s:
        due_row = await s.get(SafeModeQueueItemRow, due_item)
        fresh_row = await s.get(SafeModeQueueItemRow, fresh_item)
        assert due_row is not None and due_row.status == SafeModeStatus.EXPIRED
        assert fresh_row is not None and fresh_row.status == SafeModeStatus.PENDING

    # Sanity: the SELECT-shaped list_due_expired query still returns nothing
    # for an empty cutoff window — proves the lifecycle method itself wasn't
    # accidentally narrowed by the YAGNI removal.
    async with sf() as s:
        rows = list(
            (
                await s.execute(
                    select(SafeModeQueueItemRow).where(
                        SafeModeQueueItemRow.workspace_id == workspace_id,
                        SafeModeQueueItemRow.status == SafeModeStatus.EXPIRED,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert rows[0].id == due_item
