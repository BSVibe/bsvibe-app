"""Unit tests for :mod:`backend.workflow.application.metrics.trust_surface`.

Lift M4a. Each test seeds the in-memory SQLite stand-in for the
production schema with a focused fixture (runs + decisions + settle
drains) and asserts one metric behaviour. Goodhart resistance (design
§7) is its own test — auto-resolved Decisions must not reduce
touch_time.

Test boundaries:

* Touch time math — including the 4h clamp, window filtering, and the
  ``resolved_by IS NULL`` exclusion (goodhart).
* Deposit rate — count + slope across the daily series.
* Trend arrow logic — dormant / insufficient / rising / flat / falling.
* Contract strength — the v1 cross-check (passed verification rows
  exist for shipped runs).
* Insufficient data for new products → ``→`` per design Q7.
* Empty workspace — list_product_ids returns ``[]``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.workers.db import SettleDrainRow
from backend.workflow.application.metrics.trust_surface import (
    DEFAULT_WINDOW_DAYS,
    TOUCH_TIME_CLAMP,
    TrustSurfaceService,
)
from backend.workflow.infrastructure.db import (
    Decision,
    DecisionStatus,
    ExecutionRun,
    RunStatus,
    VerificationOutcome,
    VerificationResult,
)
from tests._support import db_engine

pytestmark = pytest.mark.asyncio


# Fixed clock far enough in the future that CI runs won't outpace the
# 14-day window for years.  All fixtures are anchored relative to _NOW;
# every test explicitly passes now=_NOW to the service methods.
_NOW = datetime(2029, 6, 1, 12, 0, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def sf():
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def workspace_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def product_id() -> uuid.UUID:
    return uuid.uuid4()


def _make_run(
    workspace_id: uuid.UUID,
    product_id: uuid.UUID | None,
    *,
    status: RunStatus = RunStatus.SHIPPED,
    created_at: datetime = _NOW,
) -> ExecutionRun:
    return ExecutionRun(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        product_id=product_id,
        status=status,
        payload={},
        created_at=created_at,
        updated_at=created_at,
    )


def _make_decision(
    workspace_id: uuid.UUID,
    run_id: uuid.UUID,
    *,
    created_at: datetime,
    resolved_at: datetime | None = None,
    resolved_by: uuid.UUID | None = None,
    status: DecisionStatus = DecisionStatus.PENDING,
) -> Decision:
    return Decision(
        id=uuid.uuid4(),
        run_id=run_id,
        workspace_id=workspace_id,
        decision="ask_user_question",
        status=status,
        created_at=created_at,
        resolved_at=resolved_at,
        resolved_by=resolved_by,
        payload={},
    )


def _make_settle(
    workspace_id: uuid.UUID,
    run_id: uuid.UUID,
    *,
    drained_at: datetime,
) -> SettleDrainRow:
    return SettleDrainRow(
        activity_id=uuid.uuid4(),
        workspace_id=workspace_id,
        run_id=run_id,
        node_ref=f"garden/seedling/{uuid.uuid4()}.md",
        drained_at=drained_at,
    )


# --- Touch time ----------------------------------------------------------


async def test_touch_time_sums_resolved_deltas(sf, workspace_id, product_id):
    """Touch time = sum of (resolved_at - created_at) clamped at 4h."""
    actor = uuid.uuid4()
    run = _make_run(workspace_id, product_id, created_at=_NOW - timedelta(days=2))
    d1 = _make_decision(
        workspace_id,
        run.id,
        created_at=_NOW - timedelta(hours=2),
        resolved_at=_NOW - timedelta(hours=1),  # 1h delta
        resolved_by=actor,
        status=DecisionStatus.RESOLVED,
    )
    d2 = _make_decision(
        workspace_id,
        run.id,
        created_at=_NOW - timedelta(hours=5),
        resolved_at=_NOW - timedelta(hours=4, minutes=30),  # 30min delta
        resolved_by=actor,
        status=DecisionStatus.RESOLVED,
    )
    async with sf() as s:
        s.add(run)
        await s.flush()
        s.add_all([d1, d2])
        await s.commit()
    async with sf() as s:
        svc = TrustSurfaceService(s)
        m = await svc.compute_touch_time(workspace_id, product_id, now=_NOW)
    assert m.decisions_resolved_count == 2
    assert m.decisions_pending_count == 0
    # 1h + 0.5h = 1.5h
    assert m.total_touch_time_hours == pytest.approx(1.5, rel=1e-3)
    assert m.window_days == DEFAULT_WINDOW_DAYS


async def test_touch_time_clamps_at_4_hours(sf, workspace_id, product_id):
    """A Decision left overnight (16h delta) is clamped at 4h."""
    actor = uuid.uuid4()
    run = _make_run(workspace_id, product_id, created_at=_NOW - timedelta(days=2))
    d_overnight = _make_decision(
        workspace_id,
        run.id,
        created_at=_NOW - timedelta(hours=20),
        resolved_at=_NOW - timedelta(hours=4),  # 16h delta
        resolved_by=actor,
        status=DecisionStatus.RESOLVED,
    )
    async with sf() as s:
        s.add(run)
        await s.flush()
        s.add(d_overnight)
        await s.commit()
    async with sf() as s:
        svc = TrustSurfaceService(s)
        m = await svc.compute_touch_time(workspace_id, product_id, now=_NOW)
    # Clamped at 4h (== TOUCH_TIME_CLAMP).
    assert m.total_touch_time_hours == pytest.approx(
        TOUCH_TIME_CLAMP.total_seconds() / 3600.0, rel=1e-3
    )


async def test_touch_time_excludes_auto_resolved(sf, workspace_id, product_id):
    """Goodhart resistance — Decisions with resolved_by=NULL don't count."""
    actor = uuid.uuid4()
    run = _make_run(workspace_id, product_id, created_at=_NOW - timedelta(days=2))
    # One human-resolved, one auto-resolved (resolved_by=None).
    d_human = _make_decision(
        workspace_id,
        run.id,
        created_at=_NOW - timedelta(hours=2),
        resolved_at=_NOW - timedelta(hours=1),
        resolved_by=actor,
        status=DecisionStatus.RESOLVED,
    )
    d_auto = _make_decision(
        workspace_id,
        run.id,
        created_at=_NOW - timedelta(hours=3),
        resolved_at=_NOW - timedelta(hours=2, minutes=30),
        resolved_by=None,  # no human touch
        status=DecisionStatus.RESOLVED,
    )
    async with sf() as s:
        s.add(run)
        await s.flush()
        s.add_all([d_human, d_auto])
        await s.commit()
    async with sf() as s:
        svc = TrustSurfaceService(s)
        m = await svc.compute_touch_time(workspace_id, product_id, now=_NOW)
    # Only the human-resolved Decision contributes — 1h, NOT 1.5h.
    assert m.decisions_resolved_count == 1
    assert m.total_touch_time_hours == pytest.approx(1.0, rel=1e-3)


async def test_touch_time_only_within_window(sf, workspace_id, product_id):
    """Decisions resolved BEFORE the window start are excluded."""
    actor = uuid.uuid4()
    run = _make_run(workspace_id, product_id, created_at=_NOW - timedelta(days=30))
    d_outside = _make_decision(
        workspace_id,
        run.id,
        # Resolved 20 days ago (window = 14 days).
        created_at=_NOW - timedelta(days=20, hours=1),
        resolved_at=_NOW - timedelta(days=20),
        resolved_by=actor,
        status=DecisionStatus.RESOLVED,
    )
    d_inside = _make_decision(
        workspace_id,
        run.id,
        created_at=_NOW - timedelta(days=1, hours=1),
        resolved_at=_NOW - timedelta(days=1),
        resolved_by=actor,
        status=DecisionStatus.RESOLVED,
    )
    async with sf() as s:
        s.add(run)
        await s.flush()
        s.add_all([d_outside, d_inside])
        await s.commit()
    async with sf() as s:
        svc = TrustSurfaceService(s)
        m = await svc.compute_touch_time(workspace_id, product_id, now=_NOW)
    assert m.decisions_resolved_count == 1
    assert m.total_touch_time_hours == pytest.approx(1.0, rel=1e-3)


async def test_touch_time_pending_counted_separately(sf, workspace_id, product_id):
    """Pending Decisions contribute to pending count, not touch_time."""
    run = _make_run(workspace_id, product_id, created_at=_NOW - timedelta(days=2))
    d_pending = _make_decision(
        workspace_id,
        run.id,
        created_at=_NOW - timedelta(hours=2),
        # No resolved_at; status pending.
        status=DecisionStatus.PENDING,
    )
    async with sf() as s:
        s.add(run)
        await s.flush()
        s.add(d_pending)
        await s.commit()
    async with sf() as s:
        svc = TrustSurfaceService(s)
        m = await svc.compute_touch_time(workspace_id, product_id, now=_NOW)
    assert m.decisions_pending_count == 1
    assert m.decisions_resolved_count == 0
    assert m.total_touch_time_hours == 0.0


# --- Deposit rate --------------------------------------------------------


async def test_deposit_rate_counts_verified_run_drains(sf, workspace_id, product_id):
    """Settle drains joined to SHIPPED runs are the deposit count."""
    shipped_run = _make_run(workspace_id, product_id, status=RunStatus.SHIPPED)
    failed_run = _make_run(workspace_id, product_id, status=RunStatus.FAILED)
    # 3 deposits on the shipped run, 1 on the failed run (should be excluded).
    drains = [
        _make_settle(workspace_id, shipped_run.id, drained_at=_NOW - timedelta(days=1)),
        _make_settle(workspace_id, shipped_run.id, drained_at=_NOW - timedelta(days=2)),
        _make_settle(workspace_id, shipped_run.id, drained_at=_NOW - timedelta(days=3)),
        _make_settle(workspace_id, failed_run.id, drained_at=_NOW - timedelta(days=1)),
    ]
    async with sf() as s:
        s.add_all([shipped_run, failed_run])
        await s.flush()
        s.add_all(list(drains))
        await s.commit()
    async with sf() as s:
        svc = TrustSurfaceService(s)
        m = await svc.compute_deposit_rate(workspace_id, product_id, now=_NOW)
    assert m.deposit_count == 3


async def test_deposit_rate_zero_when_no_drains(sf, workspace_id, product_id):
    """No drains → count 0, slope 0."""
    async with sf() as s:
        svc = TrustSurfaceService(s)
        m = await svc.compute_deposit_rate(workspace_id, product_id, now=_NOW)
    assert m.deposit_count == 0
    assert m.slope_per_day == 0.0


# --- Trend arrow ---------------------------------------------------------


async def test_trend_arrow_dormant_when_no_activity(sf, workspace_id, product_id):
    """No runs, no decisions, no drains → '·' dormant."""
    async with sf() as s:
        svc = TrustSurfaceService(s)
        arrow = await svc.compute_trend_arrow(workspace_id, product_id, now=_NOW)
    assert arrow.glyph == "·"
    assert "activity" in arrow.reason.lower()


async def test_trend_arrow_new_product_returns_flat(sf, workspace_id, product_id):
    """A product whose oldest run is <3 days shares '→' per design Q7."""
    actor = uuid.uuid4()
    # Run created very recently → insufficient data window coverage.
    run = _make_run(workspace_id, product_id, created_at=_NOW - timedelta(hours=12))
    d = _make_decision(
        workspace_id,
        run.id,
        created_at=_NOW - timedelta(hours=2),
        resolved_at=_NOW - timedelta(hours=1),
        resolved_by=actor,
        status=DecisionStatus.RESOLVED,
    )
    async with sf() as s:
        s.add(run)
        await s.flush()
        s.add(d)
        await s.commit()
    async with sf() as s:
        svc = TrustSurfaceService(s)
        arrow = await svc.compute_trend_arrow(workspace_id, product_id, now=_NOW)
    assert arrow.glyph == "→"
    assert "not enough data" in arrow.reason.lower()


async def test_trend_arrow_rising_when_ratio_falls(sf, workspace_id, product_id):
    """Older half ratio HIGH + newer half ratio LOW → '↗'."""
    actor = uuid.uuid4()
    # Older run with high touch + low deposits; newer run inverse.
    older_run = _make_run(workspace_id, product_id, created_at=_NOW - timedelta(days=20))
    newer_run = _make_run(workspace_id, product_id, created_at=_NOW - timedelta(days=2))
    # Older half — 1 long Decision (4h clamp), 1 deposit. Ratio = 240 min/dep.
    d_old = _make_decision(
        workspace_id,
        older_run.id,
        created_at=_NOW - timedelta(days=10, hours=5),
        resolved_at=_NOW - timedelta(days=10, hours=1),
        resolved_by=actor,
        status=DecisionStatus.RESOLVED,
    )
    drain_old = _make_settle(workspace_id, older_run.id, drained_at=_NOW - timedelta(days=10))
    # Newer half — 1 short Decision (6 min), 6 deposits. Ratio = 1 min/dep.
    d_new = _make_decision(
        workspace_id,
        newer_run.id,
        created_at=_NOW - timedelta(days=2, minutes=6),
        resolved_at=_NOW - timedelta(days=2),
        resolved_by=actor,
        status=DecisionStatus.RESOLVED,
    )
    drains_new = [
        _make_settle(workspace_id, newer_run.id, drained_at=_NOW - timedelta(days=i))
        for i in range(1, 7)
    ]
    async with sf() as s:
        s.add_all([older_run, newer_run])
        await s.flush()
        s.add_all([d_old, d_new, drain_old, *drains_new])
        await s.commit()
    async with sf() as s:
        svc = TrustSurfaceService(s)
        arrow = await svc.compute_trend_arrow(workspace_id, product_id, now=_NOW)
    assert arrow.glyph == "↗"


async def test_trend_arrow_falling_when_ratio_rises(sf, workspace_id, product_id):
    """Older half ratio LOW + newer half ratio HIGH → '↘'."""
    actor = uuid.uuid4()
    older_run = _make_run(workspace_id, product_id, created_at=_NOW - timedelta(days=20))
    newer_run = _make_run(workspace_id, product_id, created_at=_NOW - timedelta(days=2))
    # Older half — short Decision, many deposits. Low ratio.
    d_old = _make_decision(
        workspace_id,
        older_run.id,
        created_at=_NOW - timedelta(days=10, minutes=6),
        resolved_at=_NOW - timedelta(days=10),
        resolved_by=actor,
        status=DecisionStatus.RESOLVED,
    )
    drains_old = [
        _make_settle(workspace_id, older_run.id, drained_at=_NOW - timedelta(days=10 + i))
        for i in range(0, 4)
    ]
    # Newer half — long Decision, few deposits. High ratio.
    d_new = _make_decision(
        workspace_id,
        newer_run.id,
        created_at=_NOW - timedelta(days=2, hours=4),
        resolved_at=_NOW - timedelta(days=2),
        resolved_by=actor,
        status=DecisionStatus.RESOLVED,
    )
    drain_new = _make_settle(workspace_id, newer_run.id, drained_at=_NOW - timedelta(days=2))
    async with sf() as s:
        s.add_all([older_run, newer_run])
        await s.flush()
        s.add_all([d_old, d_new, drain_new, *drains_old])
        await s.commit()
    async with sf() as s:
        svc = TrustSurfaceService(s)
        arrow = await svc.compute_trend_arrow(workspace_id, product_id, now=_NOW)
    assert arrow.glyph == "↘"


# --- Contract strength ----------------------------------------------------


async def test_contract_strength_steady_when_runs_have_verifications(sf, workspace_id, product_id):
    """Shipped run + matching PASSED VerificationResult → steady."""
    run = _make_run(workspace_id, product_id, status=RunStatus.SHIPPED)
    run.updated_at = _NOW - timedelta(days=1)
    verif = VerificationResult(
        id=uuid.uuid4(),
        run_id=run.id,
        workspace_id=workspace_id,
        outcome=VerificationOutcome.PASSED,
        contract={},
        result={},
        created_at=_NOW - timedelta(days=1),
    )
    async with sf() as s:
        s.add(run)
        await s.flush()
        s.add(verif)
        await s.commit()
    async with sf() as s:
        svc = TrustSurfaceService(s)
        cs = await svc.compute_contract_strength(workspace_id, product_id, now=_NOW)
    assert cs.is_steady is True
    assert cs.amber_reason is None


async def test_contract_strength_amber_when_shipped_without_verifications(
    sf, workspace_id, product_id
):
    """Shipped runs but NO PASSED verifications → amber."""
    run = _make_run(workspace_id, product_id, status=RunStatus.SHIPPED)
    run.updated_at = _NOW - timedelta(days=1)
    async with sf() as s:
        s.add(run)
        await s.commit()
    async with sf() as s:
        svc = TrustSurfaceService(s)
        cs = await svc.compute_contract_strength(workspace_id, product_id, now=_NOW)
    assert cs.is_steady is False
    assert cs.amber_reason is not None


async def test_contract_strength_steady_when_no_runs_shipped(sf, workspace_id, product_id):
    """No shipped runs → default to steady (no amber) per design §2.1."""
    async with sf() as s:
        svc = TrustSurfaceService(s)
        cs = await svc.compute_contract_strength(workspace_id, product_id, now=_NOW)
    assert cs.is_steady is True
    assert cs.amber_reason is None


# --- list_product_ids -----------------------------------------------------


async def test_list_product_ids_returns_distinct_product_ids(sf, workspace_id):
    """Distinct product_ids attached to workspace runs."""
    p1 = uuid.uuid4()
    p2 = uuid.uuid4()
    other_ws = uuid.uuid4()
    r1 = _make_run(workspace_id, p1, status=RunStatus.SHIPPED)
    r2 = _make_run(workspace_id, p1, status=RunStatus.OPEN)
    r3 = _make_run(workspace_id, p2, status=RunStatus.SHIPPED)
    r4 = _make_run(workspace_id, None, status=RunStatus.OPEN)
    # Cross-workspace contamination guard.
    r5 = _make_run(other_ws, uuid.uuid4(), status=RunStatus.SHIPPED)
    async with sf() as s:
        s.add_all([r1, r2, r3, r4, r5])
        await s.commit()
    async with sf() as s:
        svc = TrustSurfaceService(s)
        ids = await svc.list_product_ids(workspace_id)
    assert set(ids) == {p1, p2}


async def test_list_product_ids_empty_workspace(sf, workspace_id):
    """Empty workspace returns ``[]``."""
    async with sf() as s:
        svc = TrustSurfaceService(s)
        ids = await svc.list_product_ids(workspace_id)
    assert ids == []


# --- compute_product_trust composite -------------------------------------


async def test_compute_product_trust_composes_all_four(sf, workspace_id, product_id):
    """The composite returns all four metrics in one shape."""
    actor = uuid.uuid4()
    run = _make_run(
        workspace_id,
        product_id,
        status=RunStatus.SHIPPED,
        created_at=_NOW - timedelta(days=20),
    )
    run.updated_at = _NOW - timedelta(days=1)
    d = _make_decision(
        workspace_id,
        run.id,
        created_at=_NOW - timedelta(hours=2),
        resolved_at=_NOW - timedelta(hours=1),
        resolved_by=actor,
        status=DecisionStatus.RESOLVED,
    )
    drain = _make_settle(workspace_id, run.id, drained_at=_NOW - timedelta(days=2))
    verif = VerificationResult(
        id=uuid.uuid4(),
        run_id=run.id,
        workspace_id=workspace_id,
        outcome=VerificationOutcome.PASSED,
        contract={},
        result={},
        created_at=_NOW - timedelta(days=1),
    )
    async with sf() as s:
        s.add(run)
        await s.flush()
        s.add_all([d, drain, verif])
        await s.commit()
    async with sf() as s:
        svc = TrustSurfaceService(s)
        pt = await svc.compute_product_trust(workspace_id, product_id, now=_NOW)
    assert pt.product_id == product_id
    assert pt.touch_time.decisions_resolved_count == 1
    assert pt.deposit_rate.deposit_count == 1
    assert pt.trend_arrow.glyph in {"↗", "→", "↘", "·"}
    assert pt.contract_strength.is_steady is True
