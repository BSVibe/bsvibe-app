"""Auto-resolve a run's paused review Decision when its deliverable ships.

Couples delivery-SUCCESS to the run's founder-review gate. A run that finishes
with WEAK verification evidence raises a ``human_review_required`` Decision
(reason ``weak_evidence_no_gate``) and PAUSES at ``RUNNING`` — the drive loop
leaves ``needs_decision`` runs running, paused on the Decision. Meanwhile the
agent's emitted Deliverable travels the OUTPUT path independently: it lands in
the Safe Mode queue (or dispatches directly when Safe Mode is off). When the
founder APPROVES the Safe Mode item the deliverable ships (e.g. a GitHub PR
opens) — but the run's ``human_review_required`` Decision stays ``pending``
forever, so the run sits ``RUNNING`` "reviewing" work that ALREADY shipped.

The two founder-gates (the run's review Decision + the Safe-Mode delivery
approval) were DECOUPLED. This module closes the gap (founder choice "B"): when
a run's Deliverable is DELIVERED (delivery success — covers the
Safe-Mode-approve→dispatch path AND direct delivery), AUTO-RESOLVE that run's
pending review Decision as moot (the work shipped, so "review before ship" is
moot) and transition the run to the terminal ``SHIPPED`` state — WITHOUT
re-delivering (already delivered) and WITHOUT minting a duplicate Deliverable
(one already exists).

The transition takes the SAME two valid state-machine hops the checkpoint
``ship`` handler uses (``RUNNING → REVIEW_READY → SHIPPED``) and marks the
latest WorkStep VERIFIED / PROVED exactly like
:func:`~backend.workflow.application.checkpoint_resolution._ship_decision_run`
does — the founder implicitly approved past the review by approving the Safe
Mode delivery, so B4 trust integrity is preserved (each resolved Decision row
records the auto-resolution + one ``ExecutionRunHistory`` row per hop captures
the transition).

The two hops are written INLINE (status + one ``ExecutionRunHistory`` row each)
rather than routed through :class:`AgentRunner` — the same direct-write pattern
:meth:`AgentRunner._auto_ship_product_run` uses for its own SHIPPED hop. Two
reasons: (1) importing ``AgentRunner`` would drag the whole run-engine graph
(``agent_loop → run_persistence → plugin.audit``) onto ``dispatch_delivery``,
which is reached from the INBOUND webhook layer the R2c contract keeps
``plugin``-free; (2) a delivery-time reconciliation must NOT trigger the
engine's REVIEW_READY side effects (``_auto_ship_product_run``'s ``merge_to_main``
git op, ``_maybe_spawn_impl_run``) — the work already shipped via the connector,
so we only reflect that by moving the run to its terminal SHIPPED state.

The helper is transaction-agnostic — it ``flush``es but never ``commit``s
(mirrors :mod:`backend.workflow.application.checkpoint_resolution`). The single
delivery-success seam (:func:`~backend.workflow.infrastructure.workers.delivery_worker.dispatch_delivery`)
owns the commit, so EVERY caller of that helper (the DeliveryWorker direct path,
the REST / MCP / Telegram Safe-Mode approve handlers) gets the auto-resolution
for free and no future caller can forget it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import select

from backend.workflow.infrastructure.db import (
    DecisionStatus,
    Deliverable,
    ExecutionRun,
    ExecutionRunHistory,
    ProofState,
    RunStatus,
    WorkStep,
    WorkStepStatus,
)
from backend.workflow.infrastructure.repositories import (
    SqlAlchemyDecisionRepository,
    SqlAlchemyRunRepository,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger(__name__)

#: The resolution marker recorded on the auto-resolved Decision row (a stable
#: wire identifier — downstream knowledge / analytics key off it, not a locale
#: label). It says "the deliverable shipped, so the review-before-ship gate is
#: moot" — distinct from a founder's explicit ``ship`` / free-text answer.
AUTO_RESOLVED_DELIVERABLE_SHIPPED = "auto_resolved_deliverable_shipped"

#: A well-known SYSTEM actor id stamped on ``Decision.resolved_by`` for an
#: auto-resolution (no human clicked). Derived deterministically so every
#: auto-resolved row carries the same stable id (queryable / auditable), and
#: distinct from any real user id.
SYSTEM_AUTO_RESOLVE_ACTOR_ID = uuid.uuid5(
    uuid.NAMESPACE_URL, "bsvibe:system:auto_resolve_deliverable_shipped"
)

#: The paused-run Decision kinds this auto-resolution closes. These are the
#: "review before we call it verified / before it ships" gates the drive loop
#: raises when a run cannot auto-accumulate trust (weak evidence) or failed
#: verification. A ``ship_or_discard`` / ``ask_user_question`` Decision is NOT
#: in this set — those are not "review the shipped work" gates.
_REVIEW_DECISION_KINDS: frozenset[str] = frozenset({"human_review_required", "verification_failed"})

#: Run statuses that are already terminal — the auto-resolution is a no-op for
#: them (idempotent: a second delivery of an already-shipped run does nothing).
_TERMINAL_RUN_STATUSES: frozenset[RunStatus] = frozenset(
    {RunStatus.SHIPPED, RunStatus.CANCELLED, RunStatus.FAILED}
)


async def auto_resolve_run_on_delivery(
    session: AsyncSession,
    *,
    deliverable_id: uuid.UUID,
) -> bool:
    """Resolve a run's pending review Decision + ship the run when its
    deliverable is delivered. Returns ``True`` iff it resolved + shipped.

    Idempotent + guarded — a no-op (returns ``False``, no writes) when:

    * the Deliverable row is missing (purged run);
    * the run is missing or already terminal (SHIPPED / CANCELLED / FAILED) —
      so a repeat delivery of an already-shipped run does nothing;
    * the run has NO pending ``human_review_required`` / ``verification_failed``
      Decision — so a run that took the normal verified → REVIEW_READY →
      SHIPPED path is never touched.

    On the happy path it: marks each such pending Decision ``RESOLVED`` with the
    :data:`AUTO_RESOLVED_DELIVERABLE_SHIPPED` marker (``resolved_at`` /
    ``resolved_by`` stamped), marks the latest WorkStep VERIFIED / PROVED, and
    transitions the run ``RUNNING → REVIEW_READY → SHIPPED`` via the same valid
    two-hop path :func:`_ship_decision_run` uses. It NEVER re-delivers and NEVER
    mints a Deliverable — one already exists (it is the one that just shipped).

    Flush-only: the caller owns the transaction boundary.
    """
    deliverable = await session.get(Deliverable, deliverable_id)
    if deliverable is None:
        return False
    run_id = deliverable.run_id
    workspace_id = deliverable.workspace_id

    runs = SqlAlchemyRunRepository(session)
    run = await runs.get(run_id)
    if run is None or run.status in _TERMINAL_RUN_STATUSES:
        return False

    decisions_repo = SqlAlchemyDecisionRepository(session)
    all_for_run = await decisions_repo.list_by_run(run_id, workspace_id)
    pending_review = [
        d
        for d in all_for_run
        if d.status is DecisionStatus.PENDING and d.decision in _REVIEW_DECISION_KINDS
    ]
    if not pending_review:
        # Normal verified path (no pending review gate) — leave the run alone.
        return False

    now = datetime.now(tz=UTC)
    for decision in pending_review:
        decision.status = DecisionStatus.RESOLVED
        decision.resolution = AUTO_RESOLVED_DELIVERABLE_SHIPPED
        decision.resolved_at = now
        decision.resolved_by = SYSTEM_AUTO_RESOLVE_ACTOR_ID

    # Mark the latest WorkStep VERIFIED / PROVED — the founder implicitly
    # approved past the review by approving the shipped delivery (mirrors
    # ``_ship_decision_run``; B4 trust integrity preserved via the Decision +
    # history audit rows). DB ProofState success terminal is PROVED.
    work_step = (
        (
            await session.execute(
                select(WorkStep)
                .where(WorkStep.run_id == run_id)
                .order_by(WorkStep.created_at.desc())
            )
        )
        .scalars()
        .first()
    )
    if work_step is not None:
        work_step.status = WorkStepStatus.VERIFIED
        work_step.proof_state = ProofState.PROVED

    # Two valid state-machine hops to the terminal SHIPPED state, always PASSING
    # THROUGH REVIEW_READY (never a RUNNING → SHIPPED illegal jump). A run paused
    # on a review Decision rests at RUNNING; the defensive REVIEW_READY branch
    # covers the rare case the run already advanced.
    reason = f"auto-resolved: deliverable {deliverable_id} shipped (review moot)"
    _ship_run_via_review_ready(session, run, reason=reason)

    await session.flush()

    # Audit trail: each hop above appended an ``ExecutionRunHistory`` row carrying
    # the ``auto-resolved …`` reason, and each resolved ``Decision`` row records
    # ``resolution`` / ``resolved_at`` / ``resolved_by`` (the SYSTEM actor) — so
    # the auto-resolution is fully provenanced. No ``plugin.audit`` emission here:
    # this helper is reached transitively from the inbound webhook layer
    # (Telegram / Slack approve → ``dispatch_delivery``), which the R2c contract
    # keeps free of any ``plugin`` edge. The founder-driven checkpoint resolve
    # service (not on the inbound path) is the one that emits ``DecisionResolved``.
    logger.info(
        "run_auto_resolved_on_delivery",
        run_id=str(run_id),
        deliverable_id=str(deliverable_id),
        resolved_decisions=len(pending_review),
    )
    return True


def _ship_run_via_review_ready(
    session: AsyncSession,
    run: ExecutionRun,
    *,
    reason: str,
) -> None:
    """Advance ``run`` to SHIPPED through the valid REVIEW_READY hop, writing a
    history row per transition.

    Direct status + ``ExecutionRunHistory`` writes (the same pattern
    :meth:`AgentRunner._auto_ship_product_run` uses) rather than routing through
    :class:`AgentRunner` — see the module docstring for why (R2c inbound
    plugin-free + no unwanted engine side effects). Caller guarantees the run is
    non-terminal (guarded upstream); a run already at REVIEW_READY only needs the
    final hop.
    """
    now = datetime.now(tz=UTC)
    if run.status is not RunStatus.REVIEW_READY:
        _record_hop(session, run, to_status=RunStatus.REVIEW_READY, reason=reason, at=now)
    _record_hop(session, run, to_status=RunStatus.SHIPPED, reason=reason, at=now)


def _record_hop(
    session: AsyncSession,
    run: ExecutionRun,
    *,
    to_status: RunStatus,
    reason: str,
    at: datetime,
) -> None:
    """Flip ``run.status`` and append the matching ``ExecutionRunHistory`` row."""
    from_status = run.status
    run.status = to_status
    run.updated_at = at
    session.add(
        ExecutionRunHistory(
            id=uuid.uuid4(),
            run_id=run.id,
            workspace_id=run.workspace_id,
            from_status=from_status,
            to_status=to_status,
            reason=reason,
            created_at=at,
        )
    )


__all__ = [
    "AUTO_RESOLVED_DELIVERABLE_SHIPPED",
    "SYSTEM_AUTO_RESOLVE_ACTOR_ID",
    "auto_resolve_run_on_delivery",
]
