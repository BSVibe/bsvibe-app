"""Proof-surface trust metrics — touch time, deposit rate, trend arrow, contract strength.

Lift M4a. The design SoT is ``~/Docs/BSVibe_Proof_Surface_Design_2026-05-30.md``;
this module implements §1 (touch_minutes + deposit_count) + §2.1 (Signal
A/B/C decomposition) + §1.3 (north-star ratio + arrow) + §2.1 goodhart
cross-check.

Every signal is computed from data already on disk — `audit_outbox` rows
(for decision pending/resolved + verify-run + loop-terminal) + the
`settle_drains` table joined to `execution_runs.status='shipped'`. No
new schema, no new audit events; per design Q4 the optional
`knowledge.retrieval.hit` event is deferred to a follow-up. Per design
Q1/Q2/Q3/Q5/Q7 (founder-confirmed) the constants here are not exposed as
runtime knobs — they're the locked v1 contract.

Goodhart resistance: per design §7, touch-time only counts Decisions
resolved by a human (``Decision.resolved_by IS NOT NULL``). Auto-resolved
Decisions (no founder touch) do NOT reduce touch time — see
:func:`compute_touch_time`.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.workers.db import SettleDrainRow
from backend.workflow.infrastructure.db import (
    Decision,
    DecisionStatus,
    ExecutionRun,
    RunStatus,
)

# --- Locked design constants (BSVibe_Proof_Surface_Design_2026-05-30 §1.3) ---

#: Per-Decision touch-time clamp per design Q1 — caps the "open overnight"
#: tail so a Decision sitting through sleep / weekend doesn't dominate.
TOUCH_TIME_CLAMP = timedelta(hours=4)

#: Default rolling window per design Q3 — 14 days reactive enough to surface
#: regressions within a sprint, smooth enough that one bad day doesn't flip
#: the arrow.
DEFAULT_WINDOW_DAYS = 14

#: Half-window length used to compute "is touch_time rising?" — we compare
#: the older half vs the newer half over the same window. 7 days each.
HALF_WINDOW_DAYS = 7

#: Minimum data per product before we emit a non-dormant glyph (design Q7).
#: ``→`` doubles as "insufficient data" with a tooltip; a 4th glyph would
#: bloat the legend with no founder upside.
MIN_DATA_DAYS = 3

#: Slope-flat threshold for the arrow glyph (design §1.3). Below ε the
#: north-star ratio is "level"; we render ``→``. Normalised so it doesn't
#: depend on absolute scale.
FLAT_EPSILON = 0.05


TrendGlyph = Literal["↗", "→", "↘", "·"]


# --- Result dataclasses (immutable wire shapes for the REST surface) -------


@dataclass(frozen=True)
class TouchTimeMetric:
    """Founder touch time for one product over the rolling window.

    ``total_touch_time_hours`` sums ``(resolved_at - created_at)`` per
    paired Decision, clamped at :data:`TOUCH_TIME_CLAMP`. Auto-resolved
    Decisions (``resolved_by IS NULL``) are excluded — design §7.
    """

    total_touch_time_hours: float
    decisions_resolved_count: int
    decisions_pending_count: int
    window_days: int


@dataclass(frozen=True)
class DepositMetric:
    """Deposit rate — verified-run garden notes deposited in the window.

    ``deposit_count`` is the count of :class:`SettleDrainRow` joined to
    verified runs (``ExecutionRun.status == 'shipped'``). ``slope_per_day``
    is the linear-regression slope across daily counts.
    """

    deposit_count: int
    slope_per_day: float
    window_days: int


@dataclass(frozen=True)
class TrendArrow:
    """L0 Fleet glyph + plain-language reason.

    The glyph is the only thing rendered on the glance per design §3.2;
    the reason backs the hover tooltip.
    """

    glyph: TrendGlyph
    reason: str


@dataclass(frozen=True)
class ContractStrength:
    """Goodhart cross-check for the L3 Inside trust panel.

    Per design §2.1 Signal A + B + §7 — a steady ``verified_share``
    combined with falling ``judge_checks`` average is the
    "system-gaming-you" tell. ``is_steady`` is ``True`` until that
    combination triggers; on trigger, ``amber_reason`` names the cause.
    """

    is_steady: bool
    amber_reason: str | None


@dataclass(frozen=True)
class ProductTrust:
    """Composite per-product trust view backing ``GET /trust/{product_id}``."""

    product_id: uuid.UUID
    touch_time: TouchTimeMetric
    deposit_rate: DepositMetric
    trend_arrow: TrendArrow
    contract_strength: ContractStrength


# --- Helpers --------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _window_start(window_days: int, now: datetime | None = None) -> datetime:
    return (now or _utcnow()) - timedelta(days=window_days)


def _as_utc(dt: datetime) -> datetime:
    """Treat naive datetimes as UTC.

    SQLite returns naive datetimes for ``TIMESTAMP WITH TIME ZONE`` columns
    (it has no native TZ type), so arithmetic against UTC-aware values
    raises ``TypeError``. Production Postgres returns aware values and the
    coercion is a no-op. The seam keeps the metric code single-typed.
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def _slope(values: list[float]) -> float:
    """Simple linear-regression slope across a daily series.

    Returns the slope of a least-squares fit ``y = a*x + b`` where ``x``
    is 0..n-1. ``0.0`` for fewer than 2 points (no direction defined).
    """
    n = len(values)
    if n < 2:
        return 0.0
    mean_x = (n - 1) / 2.0
    mean_y = sum(values) / n
    num = sum((i - mean_x) * (v - mean_y) for i, v in enumerate(values))
    den = sum((i - mean_x) ** 2 for i in range(n))
    if den == 0:
        return 0.0
    return num / den


# --- Metric service -------------------------------------------------------


class TrustSurfaceService:
    """Aggregate the four metrics for one workspace.

    Composes :class:`AsyncSession` direct queries — these are aggregation
    queries that don't fit the repository pattern (per v8 D45, "per real
    caller, never speculative" — the existing repositories surface
    single-row / list operations, not GROUP BY counts). Workspace
    isolation is enforced by `workspace_id` filters on every query plus
    the global ORM auto-filter (defense layer 2).
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def compute_touch_time(
        self,
        workspace_id: uuid.UUID,
        product_id: uuid.UUID,
        *,
        window_days: int = DEFAULT_WINDOW_DAYS,
        now: datetime | None = None,
    ) -> TouchTimeMetric:
        """Compute founder touch time for one product over the window.

        Implements design §1.1. Pairs ``Decision`` rows by id — each row
        carries its own ``created_at`` (the moment the Decision went
        ``pending``) and, when resolved, ``resolved_at`` + ``resolved_by``.
        We sum ``(resolved_at - created_at)`` clamped at
        :data:`TOUCH_TIME_CLAMP` per Decision, over Decisions whose
        ``resolved_at`` falls within the window AND whose ``resolved_by``
        is non-NULL (founder, not auto-resolution — goodhart resistance).

        Pending Decisions are counted separately so the L3 trust panel
        can render "X calls needed you" — but pending DOES NOT contribute
        touch_time (no resolved_at to subtract from).
        """
        window_start = _window_start(window_days, now)
        run_q = select(ExecutionRun.id).where(
            ExecutionRun.workspace_id == workspace_id,
            ExecutionRun.product_id == product_id,
        )
        run_ids = (await self._session.execute(run_q)).scalars().all()
        if not run_ids:
            return TouchTimeMetric(
                total_touch_time_hours=0.0,
                decisions_resolved_count=0,
                decisions_pending_count=0,
                window_days=window_days,
            )

        resolved_q = select(Decision).where(
            Decision.workspace_id == workspace_id,
            Decision.run_id.in_(run_ids),
            Decision.status == DecisionStatus.RESOLVED,
            Decision.resolved_at.is_not(None),
            Decision.resolved_at >= window_start,
            Decision.resolved_by.is_not(None),  # goodhart resistance
        )
        resolved_rows = (await self._session.execute(resolved_q)).scalars().all()

        pending_q = select(func.count(Decision.id)).where(
            Decision.workspace_id == workspace_id,
            Decision.run_id.in_(run_ids),
            Decision.status == DecisionStatus.PENDING,
        )
        pending_count = int((await self._session.execute(pending_q)).scalar_one() or 0)

        total = timedelta(0)
        clamp = TOUCH_TIME_CLAMP
        for d in resolved_rows:
            # resolved_at is non-NULL via the WHERE clause but mypy needs the hint.
            assert d.resolved_at is not None
            delta = _as_utc(d.resolved_at) - _as_utc(d.created_at)
            if delta < timedelta(0):
                continue
            total += min(delta, clamp)

        return TouchTimeMetric(
            total_touch_time_hours=total.total_seconds() / 3600.0,
            decisions_resolved_count=len(resolved_rows),
            decisions_pending_count=pending_count,
            window_days=window_days,
        )

    async def compute_deposit_rate(
        self,
        workspace_id: uuid.UUID,
        product_id: uuid.UUID,
        *,
        window_days: int = DEFAULT_WINDOW_DAYS,
        now: datetime | None = None,
    ) -> DepositMetric:
        """Deposit count + slope over the window.

        Implements design §1.2. Joins :class:`SettleDrainRow` to
        verified runs (``ExecutionRun.status == 'shipped'``, the §3
        terminal state for a passed verification). ``drained_at`` is the
        SettleWorker's commit timestamp — exactly when the deposit
        landed.

        Slope is computed across the per-day deposit count series so
        "deposits up vs. flat vs. down" reads off the trend, not the
        absolute count.
        """
        window_start = _window_start(window_days, now)
        rows_q = (
            select(SettleDrainRow.drained_at)
            .join(ExecutionRun, SettleDrainRow.run_id == ExecutionRun.id)
            .where(
                SettleDrainRow.workspace_id == workspace_id,
                ExecutionRun.product_id == product_id,
                ExecutionRun.status == RunStatus.SHIPPED,
                SettleDrainRow.drained_at >= window_start,
            )
        )
        rows = (await self._session.execute(rows_q)).scalars().all()

        if not rows:
            return DepositMetric(
                deposit_count=0,
                slope_per_day=0.0,
                window_days=window_days,
            )

        # Bucket by day relative to window_start so the daily series has
        # one entry per window day (zeros included → slope reflects gaps).
        ref_now = now or _utcnow()
        per_day: dict[int, int] = defaultdict(int)
        for drained_at in rows:
            day_index = (_as_utc(drained_at) - window_start).days
            if 0 <= day_index < window_days:
                per_day[day_index] += 1
        daily_series = [float(per_day.get(i, 0)) for i in range(window_days)]
        # Defensive: ensure ref_now-bounded — unused beyond this assertion in
        # the current impl but keeps the call signature stable for tests.
        assert ref_now >= window_start

        return DepositMetric(
            deposit_count=len(rows),
            slope_per_day=_slope(daily_series),
            window_days=window_days,
        )

    async def compute_trend_arrow(
        self,
        workspace_id: uuid.UUID,
        product_id: uuid.UUID,
        *,
        window_days: int = DEFAULT_WINDOW_DAYS,
        now: datetime | None = None,
    ) -> TrendArrow:
        """Derive the L0 Fleet glyph from touch_time + deposit_rate.

        Implements design §1.3 north-star: the arrow tracks the trend in
        ``touch_minutes / deposit_count``. Lower ratio = each deposit
        bought less founder labour = trust rising.

        Rules:
          * Zero data (no decisions resolved AND no deposits) → ``·``.
          * Insufficient data (deposits + decisions exist but data
            covers fewer than :data:`MIN_DATA_DAYS`) → ``→`` with
            "insufficient_data" reason (design Q7).
          * Ratio falling (touch down OR deposits up faster) → ``↗``.
          * Ratio flat within ε → ``→``.
          * Ratio rising → ``↘``.
        """
        touch = await self.compute_touch_time(
            workspace_id, product_id, window_days=window_days, now=now
        )
        deposit = await self.compute_deposit_rate(
            workspace_id, product_id, window_days=window_days, now=now
        )

        # Dormant: nothing happened for this product in the window.
        if (
            touch.decisions_resolved_count == 0
            and touch.decisions_pending_count == 0
            and deposit.deposit_count == 0
        ):
            return TrendArrow(glyph="·", reason="no activity in window")

        # Insufficient data — first MIN_DATA_DAYS of activity for a new product
        # shares the flat glyph (design Q7) with a distinguishing reason.
        oldest_event = await self._oldest_event_age_days(workspace_id, product_id, now=now)
        if oldest_event is not None and oldest_event < MIN_DATA_DAYS:
            return TrendArrow(glyph="→", reason="new product · not enough data yet")

        # Split the window into older / newer halves and compute the ratio
        # delta. "rising trust" means ratio fell from older to newer.
        ref_now = now or _utcnow()
        mid = ref_now - timedelta(days=HALF_WINDOW_DAYS)
        older = await self._half_window_ratio(
            workspace_id, product_id, start=ref_now - timedelta(days=window_days), end=mid
        )
        newer = await self._half_window_ratio(workspace_id, product_id, start=mid, end=ref_now)

        # Undefined ratio in either half → fall back to flat (one or both
        # halves had zero deposits). This collapses two previous branches
        # into one — neither was meaningfully distinguishable on the glance,
        # and the tooltip already names which half is missing via the reason.
        if older is None or newer is None:
            reason = (
                "ratio undefined this window"
                if older is None and newer is None
                else "partial-window ratio"
            )
            return TrendArrow(glyph="→", reason=reason)

        if abs(older) < 1e-9:
            relative_change = newer - older
        else:
            relative_change = (newer - older) / abs(older)

        if abs(relative_change) < FLAT_EPSILON:
            return TrendArrow(glyph="→", reason="north-star ratio steady")
        if relative_change < 0:
            return TrendArrow(glyph="↗", reason="touch ÷ deposits falling — trust rising")
        return TrendArrow(glyph="↘", reason="touch ÷ deposits rising — needs attention")

    async def compute_contract_strength(
        self,
        workspace_id: uuid.UUID,
        product_id: uuid.UUID,
        *,
        window_days: int = DEFAULT_WINDOW_DAYS,
        now: datetime | None = None,
    ) -> ContractStrength:
        """Goodhart cross-check — is the verification contract holding?

        v1 simplification (per the scope-control deferral note in the
        delegation): rather than diffing serialized contract shapes
        across runs, the v1 check uses the existing audit-event proxy
        signal — count of verified runs in the window. If the workspace
        has shipped runs but **zero** of them carry a recorded
        verification result in this window, we treat that as the
        "contract weakened" tell (steady-state assumption is that
        verified-shipped runs continue to record :class:`VerificationResult`
        rows). For the v1 surface this is honest and stable; the deeper
        contract-shape diff lands when (a) the L3 trust panel surfaces
        it, and (b) we have a real founder report of the failure mode.
        """
        from backend.workflow.infrastructure.db import (  # noqa: PLC0415
            VerificationOutcome,
            VerificationResult,
        )

        window_start = _window_start(window_days, now)
        runs_q = select(ExecutionRun.id).where(
            ExecutionRun.workspace_id == workspace_id,
            ExecutionRun.product_id == product_id,
            ExecutionRun.status == RunStatus.SHIPPED,
            ExecutionRun.updated_at >= window_start,
        )
        run_ids = (await self._session.execute(runs_q)).scalars().all()
        if not run_ids:
            # Nothing shipped — nothing to cross-check; we can't honestly
            # claim "steady" so we default to steady (no amber) per
            # design §2.1 (amber-only-when-triggered).
            return ContractStrength(is_steady=True, amber_reason=None)

        passed_q = select(func.count(VerificationResult.id)).where(
            VerificationResult.workspace_id == workspace_id,
            VerificationResult.run_id.in_(run_ids),
            VerificationResult.outcome == VerificationOutcome.PASSED,
        )
        passed = int((await self._session.execute(passed_q)).scalar_one() or 0)

        if passed == 0:
            return ContractStrength(
                is_steady=False,
                amber_reason="shipped runs without recorded verification",
            )
        return ContractStrength(is_steady=True, amber_reason=None)

    async def compute_product_trust(
        self,
        workspace_id: uuid.UUID,
        product_id: uuid.UUID,
        *,
        window_days: int = DEFAULT_WINDOW_DAYS,
        now: datetime | None = None,
    ) -> ProductTrust:
        """One-shot view backing ``GET /trust/{product_id}``."""
        touch = await self.compute_touch_time(
            workspace_id, product_id, window_days=window_days, now=now
        )
        deposit = await self.compute_deposit_rate(
            workspace_id, product_id, window_days=window_days, now=now
        )
        arrow = await self.compute_trend_arrow(
            workspace_id, product_id, window_days=window_days, now=now
        )
        strength = await self.compute_contract_strength(
            workspace_id, product_id, window_days=window_days, now=now
        )
        return ProductTrust(
            product_id=product_id,
            touch_time=touch,
            deposit_rate=deposit,
            trend_arrow=arrow,
            contract_strength=strength,
        )

    async def list_product_ids(self, workspace_id: uuid.UUID) -> list[uuid.UUID]:
        """Distinct product_ids attached to runs in this workspace.

        Powers ``GET /trust/fleet`` — the workspace's "Your products"
        lane. Returns only non-NULL product_ids (runs without a product
        binding don't appear on Fleet glyph rows).
        """
        q = (
            select(ExecutionRun.product_id)
            .where(
                ExecutionRun.workspace_id == workspace_id,
                ExecutionRun.product_id.is_not(None),
            )
            .distinct()
        )
        rows = (await self._session.execute(q)).scalars().all()
        # Distinct preserves uuid sort-stability via DB ordering;
        # callers shouldn't rely on order beyond "the workspace's
        # product set". Sort for deterministic response shape.
        return sorted([r for r in rows if r is not None])

    # --- Internal helpers ------------------------------------------------

    async def _oldest_event_age_days(
        self,
        workspace_id: uuid.UUID,
        product_id: uuid.UUID,
        *,
        now: datetime | None = None,
    ) -> float | None:
        """Days since the oldest signal-bearing row for this product.

        Returns ``None`` when no rows exist at all (lets the caller
        distinguish dormant from new-product).
        """
        ref_now = now or _utcnow()
        run_q = select(func.min(ExecutionRun.created_at)).where(
            ExecutionRun.workspace_id == workspace_id,
            ExecutionRun.product_id == product_id,
        )
        oldest = (await self._session.execute(run_q)).scalar_one_or_none()
        if oldest is None:
            return None
        delta = ref_now - _as_utc(oldest)
        return delta.total_seconds() / 86400.0

    async def _half_window_ratio(
        self,
        workspace_id: uuid.UUID,
        product_id: uuid.UUID,
        *,
        start: datetime,
        end: datetime,
    ) -> float | None:
        """Compute ``touch_minutes / deposit_count`` for a half-window.

        Returns ``None`` when there are no deposits in the half (ratio
        undefined). Touch time is in MINUTES so the ratio has units of
        "minutes per deposit", matching design §1.3.
        """
        # Touch time over [start, end) — same goodhart filter as the
        # primary ``compute_touch_time``.
        run_q = select(ExecutionRun.id).where(
            ExecutionRun.workspace_id == workspace_id,
            ExecutionRun.product_id == product_id,
        )
        run_ids = (await self._session.execute(run_q)).scalars().all()
        if not run_ids:
            return None

        resolved_q = select(Decision).where(
            Decision.workspace_id == workspace_id,
            Decision.run_id.in_(run_ids),
            Decision.status == DecisionStatus.RESOLVED,
            Decision.resolved_at.is_not(None),
            Decision.resolved_at >= start,
            Decision.resolved_at < end,
            Decision.resolved_by.is_not(None),
        )
        resolved_rows = (await self._session.execute(resolved_q)).scalars().all()
        total = timedelta(0)
        for d in resolved_rows:
            assert d.resolved_at is not None
            delta = _as_utc(d.resolved_at) - _as_utc(d.created_at)
            if delta < timedelta(0):
                continue
            total += min(delta, TOUCH_TIME_CLAMP)
        touch_minutes = total.total_seconds() / 60.0

        dep_q = (
            select(func.count(SettleDrainRow.activity_id))
            .join(ExecutionRun, SettleDrainRow.run_id == ExecutionRun.id)
            .where(
                SettleDrainRow.workspace_id == workspace_id,
                ExecutionRun.product_id == product_id,
                ExecutionRun.status == RunStatus.SHIPPED,
                SettleDrainRow.drained_at >= start,
                SettleDrainRow.drained_at < end,
            )
        )
        deposits = int((await self._session.execute(dep_q)).scalar_one() or 0)
        if deposits == 0:
            return None
        return touch_minutes / deposits


__all__ = [
    "DEFAULT_WINDOW_DAYS",
    "FLAT_EPSILON",
    "HALF_WINDOW_DAYS",
    "MIN_DATA_DAYS",
    "TOUCH_TIME_CLAMP",
    "ContractStrength",
    "DepositMetric",
    "ProductTrust",
    "TouchTimeMetric",
    "TrendArrow",
    "TrendGlyph",
    "TrustSurfaceService",
]
