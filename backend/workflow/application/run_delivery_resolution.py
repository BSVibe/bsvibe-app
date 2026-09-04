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

The two founder-gates (the run's review Decision + the Safe-Mode delivery
approval) were DECOUPLED. This module closes the gap (founder choice "B"): when
a run's Deliverable is DELIVERED (delivery success — covers the
Safe-Mode-approve→dispatch path AND direct delivery), AUTO-RESOLVE that run's
pending review Decision as moot (the work shipped, so "review before ship" is
moot) and transition the run to the terminal ``SHIPPED`` state — WITHOUT
re-delivering (already delivered) and WITHOUT minting a duplicate Deliverable
(one already exists).

Second gap, same shape, no pending Decision involved (found live 2026-09-04,
run ``10e8b5f1``): a github-bound product run has NO in-process path to
``SHIPPED`` at all. ``AgentRunner.transition``'s REVIEW_READY branch only calls
``_auto_ship_product_run`` (the local ``merge_to_main`` fast-forward) when
:func:`~backend.workflow.application.agent_runner.delivers_via_local_product_repo`
is ``True`` — and it is explicitly ``False`` for a github-bound run (the local
fast-forward would only fail there, issue #362). So a clean run (verification
passed, zero pending Decisions) sits at ``REVIEW_READY`` forever: there is no
worker, no tick, nothing else that ever moves it past that status. Approving it
in Safe Mode opens (and can merge) the PR, but the run itself never learns.
This module is reused for that case too — see the ``product_id is not None and
not delivers_via_local_product_repo(...)`` branch below — because it is
already the single seam every delivery-success path (worker / REST / MCP /
Telegram) funnels through, and because "the deliverable shipped" is exactly
the same terminal-worthy fact whether or not a review Decision happened to be
pending. **Layer choice:** this was NOT put in ``dispatch_delivery`` as a
parallel seam, and NOT left to ``merge_watch_worker`` (PR-merge time), on
purpose:

* A parallel seam in ``dispatch_delivery`` would duplicate the exact
  terminal/idempotency guards this function already has (terminal-status
  check, flush-only transaction discipline) for what is really the same
  decision ("this Deliverable just shipped — does the run need moving to
  SHIPPED right now, and how").
* ``merge_watch_worker`` answers a DIFFERENT question — "has the PR been
  merged" — for the auto-merge feature, and is not present/wired for every
  delivery. Gating run-terminal on it would mean a run with auto-merge off
  (or one whose PR the founder merges by hand, or merges with unrelated CI
  still pending) never reaches SHIPPED either. **Delivery success (the PR
  successfully opened) is the product-meaning boundary "shipped" already uses
  elsewhere in this file** — the sibling pending-Decision branch above ships
  the run the moment the PR opens too, not when it merges — so this is the
  one place a single run-status invariant ("a delivered run is terminal") can
  be enforced without inventing a second, later definition of "shipped" that
  would only apply to github-bound runs.

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
      Decision AND it has (or could have) an in-process path off REVIEW_READY —
      i.e. it is not product-bound, or it delivers via the local product repo
      (:func:`~backend.workflow.application.agent_runner.delivers_via_local_product_repo`
      is ``True``, so ``AgentRunner._auto_ship_product_run`` owns getting it to
      SHIPPED). Left alone either way: a normal verified run finishes its OWN
      path, and a local-repo run's ship depends on an actual git merge this
      module must never fake.

    Otherwise — a pending review Decision, OR a product-bound run with no
    local-repo auto-ship path (github-bound) — it: marks each pending Decision
    (if any) ``RESOLVED`` with the :data:`AUTO_RESOLVED_DELIVERABLE_SHIPPED`
    marker (``resolved_at`` / ``resolved_by`` stamped), marks the latest
    WorkStep VERIFIED / PROVED, and transitions the run ``RUNNING`` (or
    ``REVIEW_READY``) ``→ REVIEW_READY → SHIPPED`` via the same valid two-hop
    path :func:`_ship_run_via_review_ready` uses. It NEVER re-delivers and NEVER
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
    if not pending_review and not await _stranded_without_local_auto_ship(session, run):
        # Normal verified path (no pending review gate), or a product-bound
        # run whose OWN in-process auto-ship at REVIEW_READY owns getting it
        # to SHIPPED (local merge_to_main) — leave the run alone either way.
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
    # THROUGH REVIEW_READY (never a RUNNING → SHIPPED illegal jump). A run
    # paused on a review Decision rests at RUNNING (both hops fire); a
    # github-bound run stranded with no pending Decision already sits at
    # REVIEW_READY (only the final hop fires — see ``_ship_run_via_review_ready``).
    reason = (
        f"auto-resolved: deliverable {deliverable_id} shipped (review moot)"
        if pending_review
        else f"auto-resolved: deliverable {deliverable_id} shipped (no local auto-ship path)"
    )
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


async def _stranded_without_local_auto_ship(session: AsyncSession, run: ExecutionRun) -> bool:
    """True iff ``run`` has NO in-process path off ``REVIEW_READY`` at all, so a
    delivery-success event is the ONLY remaining signal it can ever ship.

    Scoped to ``run.product_id is not None`` — a non-product run keeps the
    pre-existing "leave it at REVIEW_READY" invariant (matches
    ``AgentRunner.transition``'s own doc: "Non-product runs ... transition to
    REVIEW_READY and stay there unchanged"), since this module has no basis to
    invent a different one for it.

    For a product-bound run, delegates the ownership question to the SAME
    predicate ``AgentRunner.transition`` gates its own auto-ship call on
    (:func:`~backend.workflow.application.agent_runner.delivers_via_local_product_repo`)
    so the two can never disagree about which runs own their own ship. When
    that predicate is ``True`` (local repo — incl. a run mid-retry after a
    busy lock / merge failure) this returns ``False``: that run's ship depends
    on an actual ``merge_to_main``, which only ``_auto_ship_product_run`` may
    run — faking it here would ship a run whose work was never actually
    merged.

    Imported lazily (function-local, not at module import time) for the same
    reason the module docstring gives for not importing :class:`AgentRunner`
    outright: this helper is reached from the inbound webhook layer, which the
    R2c contract keeps light. ``delivers_via_local_product_repo`` is a plain
    function (not a method), so importing it alone does not drag the engine
    graph — but it lazy-imports the connector/DB lookups it needs, and keeping
    the import here (rather than at module scope) keeps that cost off every
    other caller of this module.
    """
    if run.product_id is None:
        return False
    from backend.workflow.application.agent_runner import (  # noqa: PLC0415
        delivers_via_local_product_repo,
    )

    return not await delivers_via_local_product_repo(session, run)


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
