"""Auto-resolve a run's paused review Decision when its deliverable ships.

Couples delivery-SUCCESS to the run's founder-review gate. A run that ends on a
``human_review_required`` / ``verification_failed`` Decision PAUSES at
``RUNNING`` — the drive loop leaves ``needs_decision`` runs running, paused on
the Decision. Meanwhile the
agent's emitted Deliverable travels the OUTPUT path independently: it lands in
the Safe Mode queue (or dispatches directly when Safe Mode is off). When the
founder APPROVES the Safe Mode item the deliverable ships (e.g. a GitHub PR
opens) — but the run's ``human_review_required`` Decision stays ``pending``
forever, so the run sits ``RUNNING`` "reviewing" work that ALREADY shipped.

"Delivery success" is deliberately the ships-a-PR boundary, NOT the
PR-gets-merged boundary. That distinction matters because
:class:`~backend.workflow.infrastructure.workers.merge_watch_worker.MergeWatchWorker`
is not a passive observer here: once a watched PR's checks go green it
actually calls ``merge_pr(owner, repo, number, method="squash")`` under a
per-repo advisory lock, so for a PR it is watching the merge is real, not
just detected. Gating this module's run-termination on THAT merge anyway
would strand every run whose delivery is not wired into the merge-watch
queue, whose repo has GitHub auto-merge disabled, or whose PR a human merges
by hand — none of those ever produce a merge event this module could key
off. Delivery success (the PR opening) is the boundary every delivery path
reaches; a squash merge is not.

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

#886-redo — a github-bound product run reaches REVIEW_READY WITHOUT any
pending review Decision (the normal verified path) and WITHOUT auto-shipping
locally (``AgentRunner.transition``'s ``delivers_via_local_product_repo`` gate
skips the local ``merge_to_main`` for it — issue #362, it delivers via
push+PR instead). Nothing used to ever move that run past REVIEW_READY: this
module's ``if not pending_review: return False`` early-return treated it
exactly like an already-locally-shipped run, so once its Deliverable's PR
actually opened, the run sat REVIEW_READY forever (prod: runs ``10e8b5f1``,
``96edde43``). :func:`auto_resolve_run_on_delivery` now also completes THAT
run's SHIPPED transition on delivery success — but it does so by READING the
verdict ``AgentRunner.transition`` already recorded on
``run.payload["delivers_via_local_product_repo"]``, never by recomputing the
predicate itself (that predicate transitively imports
``delivery.connector_dispatch``, which reaches ``backend.extensions`` /
``backend.router.accounts.crypto`` — modules import-linter's "MCP context
depends only on Identity + Workflow + Knowledge + common" contract forbids
reaching from this module's callers; a lazy/function-local import does NOT
avoid this, `import-linter` walks function-local imports into its static
graph too — confirmed red with ``uv run lint-imports`` on a trial import
before this fix). A run that reached REVIEW_READY before this payload key
existed (or any non-product run, whose recorded verdict is always False
too — see the ``run.product_id is not None`` guard below) has no recorded
answer or an inapplicable one; ``_should_ship_review_ready_without_local_auto_ship``
FAILS CLOSED for both, matching the founder's explicit call: shipping a run
with no evidence risks terminating one whose product repo never actually got
merged, and a stuck pre-fix run is cheap for a human to ``discard`` — only
NEW runs are guaranteed not to get stuck by this fix.

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
    TERMINAL_RUN_STATUSES,
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
_TERMINAL_RUN_STATUSES: frozenset[RunStatus] = TERMINAL_RUN_STATUSES


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
        # No paused review gate. Either (a) the normal verified path on a run
        # that auto-ships LOCALLY — already SHIPPED by
        # ``AgentRunner._auto_ship_product_run``, filtered out by the
        # terminal-status guard above — or (b) a github-bound run that
        # reached REVIEW_READY with NO local auto-ship path (#886-redo, see
        # module docstring). Complete (b) here; (a) and every other shape are
        # a no-op.
        if not _should_ship_review_ready_without_local_auto_ship(run):
            return False
        reason = f"auto-resolved: deliverable {deliverable_id} shipped (no local auto-ship path)"
        _record_hop(
            session, run, to_status=RunStatus.SHIPPED, reason=reason, at=datetime.now(tz=UTC)
        )
        await session.flush()
        logger.info(
            "run_auto_resolved_on_delivery_no_local_ship",
            run_id=str(run_id),
            deliverable_id=str(deliverable_id),
        )
        return True

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


def _recorded_local_auto_ship(run: ExecutionRun) -> bool | None:
    """The ``delivers_via_local_product_repo`` verdict ``AgentRunner.transition``
    recorded on ``run.payload`` when this run reached REVIEW_READY, or
    ``None`` if it was never recorded (a run that reached REVIEW_READY before
    this payload key existed).

    Reads ONLY the payload — never recomputes the predicate (see module
    docstring for why importing it here is off-limits).
    """
    payload = run.payload if isinstance(run.payload, dict) else {}
    value = payload.get("delivers_via_local_product_repo")
    return value if isinstance(value, bool) else None


def _should_ship_review_ready_without_local_auto_ship(run: ExecutionRun) -> bool:
    """True iff THIS run is the #886-redo case: REVIEW_READY, product-bound,
    and recorded (at its REVIEW_READY transition) as having NO local
    auto-ship path — so a successful delivery is the only remaining signal
    that can complete its SHIPPED transition.

    False for every other shape, each deliberately fail-closed / hands-off:

    * not REVIEW_READY (still RUNNING, paused on a non-review Decision such
      as ``ask_user_question``) — nothing to complete yet.
    * ``run.product_id is None`` — a non-product run never had a local
      auto-ship path to skip in the first place; its pre-#886-redo invariant
      (REVIEW_READY is not touched here) is unchanged.
    * recorded verdict is ``True`` — local auto-ship owns this run's SHIPPED
      transition (already done, or its own retry path will do it); touching
      it here would race that path.
    * recorded verdict is ``None`` (missing) — a pre-fix run. FAIL CLOSED:
      there is no way to tell whether it already shipped locally, and a
      human ``discard`` is the safe way to clear a stuck one.
    """
    if run.status is not RunStatus.REVIEW_READY:
        return False
    if run.product_id is None:
        return False
    return _recorded_local_auto_ship(run) is False


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
