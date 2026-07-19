"""Checkpoint resolve — the run-blocking-Decision resolution service.

The single source of the founder-resolves-a-paused-run-Decision logic, shared
by the REST endpoint (:mod:`backend.api.v1.checkpoints`) and — from C2 — the
MCP checkpoint tools. Extracted verbatim from the REST handler so MCP can reuse
it without crossing the ``backend.mcp`` → ``backend.api`` import boundary
(``backend.api`` is forbidden to ``backend.mcp``; this module lives in the
Workflow application layer, which MCP may import).

Behaviour mirrors ``POST /api/v1/checkpoints/{id}/resolve``: record the
founder's answer on the Decision, fold it into the run's
``payload["resolved_decisions"]``, knowledge-ize the resolution via settle
activities, emit the ``DecisionResolved`` audit event, and dispatch the action
(``ship`` / ``discard`` / free-text resume RUNNING → OPEN).

The caller owns the transaction boundary: this service ``flush``es but never
``commit``s (mirrors :mod:`backend.workflow.application.run_cleanup`). The
heavy ``AgentRunner`` engine and the git-backed ``product_workspace`` module
are lazy-imported inside the functions so importing this module stays cheap and
MCP-import-legal.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import select

from backend.workflow.application._checkpoint_shared import (
    ACTION_DISCARD,
    ACTION_SHIP,
    _decision_actions,
    _decision_options,
    _question_text,
)
from backend.workflow.application.audit_events import DecisionResolved
from backend.workflow.domain.verified_deliverable import settle_run_context
from backend.workflow.infrastructure.db import (
    Decision,
    DecisionStatus,
    Deliverable,
    DeliverableType,
    ExecutionRun,
    ExecutionRunActivity,
    ProofState,
    RunStatus,
    WorkStep,
    WorkStepStatus,
)
from backend.workflow.infrastructure.repositories import (
    SqlAlchemyDecisionRepository,
    SqlAlchemyDeliverableRepository,
    SqlAlchemyRunRepository,
)
from plugin.audit.events import AuditActor, AuditResource
from plugin.audit.service import safe_emit

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from backend.workflow.application.agent_runner import AgentRunner
    from backend.workflow.domain.repositories import DeliverableRepository

#: Payload ``kind`` on the settle activity emitted by the resolve endpoint
#: (B11b). The :class:`~backend.knowledge.infrastructure.workers.settle_worker.SettleWorker` drains the
#: row into the workspace's BSage vault — turning the answered Decision into
#: reusable knowledge so a future run with similar signals doesn't re-ask the
#: same question. The kind is stable wire shape; downstream consumers
#: (retriever, audit) key off it.
DECISION_RESOLUTION_SETTLE_KIND = "decision_resolution"

#: Payload ``kind`` on the *negative-pattern* settle activity the resolve
#: endpoint emits when the founder DISCARDS a deliverable with a reason (G1).
#: The same settle pipeline absorbs it into the vault as a ``negative_pattern``
#: garden note; the
#: :class:`~backend.knowledge.retrieval.negative_pattern_retriever.NegativePatternRetriever`
#: surfaces it as "avoid this" guidance for a future run with similar signals —
#: so a rejected approach is not silently repeated. Additive to (never replaces)
#: the ``decision_resolution`` row.
NEGATIVE_PATTERN_SETTLE_KIND = "negative_pattern"

#: Cap on the settle-activity ``summary`` text — keeps the absorbed garden
#: note's body proportionate to the question + answer (mirrors
#: :data:`~backend.workflow.domain.verified_deliverable._SETTLE_SUMMARY_CAP`).
_SUMMARY_CAP = 500

logger = structlog.get_logger(__name__)


class CheckpointResolutionError(Exception):
    """Base for the resolve service's caller-facing failures.

    Each subclass carries a human-legible ``detail`` string the REST surface
    maps to a status code verbatim (so the wire response is unchanged from the
    former inline handler)."""


class CheckpointNotFound(CheckpointResolutionError):
    """The Decision (or its run) is not a pending checkpoint in the workspace
    → REST maps to 404."""


class InvalidAction(CheckpointResolutionError):
    """The action_key / answer combination is not valid for the Decision kind
    → REST maps to 400."""


class ProductWorkspaceBusy(CheckpointResolutionError):
    """The product workspace lock was held during a ``ship`` force-merge
    → REST maps to 503 (the founder retries)."""


class ProductWorkspaceMergeFailed(CheckpointResolutionError):
    """The ``ship`` force-merge onto main failed (git non-zero)
    → REST maps to 500."""


@dataclass
class CheckpointResolutionOutcome:
    """Result of :func:`resolve_checkpoint` — the fields the REST
    ``ResolveResponse`` (and the future MCP tool) render."""

    decision_id: uuid.UUID
    run_id: uuid.UUID
    status: DecisionStatus
    resolution: str
    resolved_at: datetime
    run_status: RunStatus


async def resolve_checkpoint(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    checkpoint_id: uuid.UUID,
    answer: str = "",
    action_key: str | None = None,
    reason: str = "",
    actor_id: uuid.UUID,
    language: str = "en",
) -> CheckpointResolutionOutcome:
    """Resolve a pending Decision with the founder's answer and resume the run.

    Raises :class:`CheckpointNotFound` when the Decision is not in the caller's
    workspace or is not pending (or its run is missing). On success: record the
    answer on the Decision, append it to the run's ``payload["resolved_decisions"]``,
    and dispatch the action — free-text/retry resumes the run RUNNING → OPEN so
    the worker re-picks it; ``ship`` / ``discard`` run the side-effecting
    handlers. The caller owns the transaction (this flushes, never commits).
    """
    decisions = SqlAlchemyDecisionRepository(session)
    runs = SqlAlchemyRunRepository(session)
    deliverables = SqlAlchemyDeliverableRepository(session)

    decision = await decisions.get(checkpoint_id)
    if (
        decision is None
        or decision.workspace_id != workspace_id
        or decision.status is not DecisionStatus.PENDING
    ):
        raise CheckpointNotFound(f"Pending checkpoint {checkpoint_id} not found")

    # L-D2: validate ``action_key`` against the Decision kind's allowlist BEFORE
    # any side effects. An unknown key → 400 (the Decision stays pending). An
    # action key on a Decision that has no actions → 400.
    available_actions = _decision_actions(decision)
    if action_key is not None:
        if available_actions is None:
            raise InvalidAction(f"Decision kind {decision.decision!r} has no one-click actions")
        if action_key not in {a.key for a in available_actions}:
            raise InvalidAction(
                f"action_key {action_key!r} is not allowed for {decision.decision!r}"
            )
    elif not answer.strip():
        # Free-text path requires a non-empty answer (was previously enforced
        # via Pydantic min_length=1; relaxed because action-only resolves
        # skip ``answer``).
        raise InvalidAction("answer must be non-empty when no action_key is given")

    # L-D1: the work LLM's ``options`` are **suggestions**, not a closed
    # set. The founder may pick one of the offered strings (PWA single-
    # select) OR type their own answer ("Other" free-text) — mirrors the
    # AskUserQuestion UX where users can always fall back to free input.
    # The off-list answer is recorded verbatim as the resolution; the
    # downstream loop sees the founder's exact words, not a coerced match.
    now = datetime.now(tz=UTC)
    # L-D2: when an action_key is used, the recorded resolution is the action
    # key itself (a stable wire identifier) — not the localized label and not
    # the empty answer string. The settle activity and audit events reference
    # the key so downstream knowledge / analytics stays locale-independent.
    resolution_text = action_key if action_key is not None else answer
    decision.status = DecisionStatus.RESOLVED
    decision.resolution = resolution_text
    decision.resolved_at = now
    decision.resolved_by = actor_id

    run = await runs.get(decision.run_id)
    if run is None:
        raise CheckpointNotFound(f"Run {decision.run_id} for checkpoint not found")

    # Fold the resolution into the run payload so the loop seeds it as context.
    # Re-assign payload (not in-place mutate) so SQLAlchemy detects the change
    # on a JSON column.
    payload: dict[str, Any] = dict(run.payload or {})
    resolved = list(payload.get("resolved_decisions") or [])
    resolved.append(
        {
            "decision_id": str(decision.id),
            "question": _question_text(decision, language),
            "answer": resolution_text,
        }
    )
    payload["resolved_decisions"] = resolved
    run.payload = payload

    await session.flush()

    # B11b — Knowledge-ize the resolution. Emit a ``settle`` ExecutionRunActivity
    # carrying the decision-resolution payload + the run's stable clustering
    # context (intent/product). The :class:`~backend.knowledge.infrastructure.workers.settle_worker.SettleWorker`
    # drains this row into the workspace's BSage vault, exactly like a
    # verified-work observation — so a future run with similar signals can
    # surface the prior decision via the retriever (the SAME seam B3 verify
    # and B6 seed inject). ``verified`` is False — the resolution is an honest
    # answer, NOT verified-as-code (B4 trust integrity).
    settle_payload: dict[str, Any] = {
        "kind": DECISION_RESOLUTION_SETTLE_KIND,
        "decision_id": str(decision.id),
        "question": _question_text(decision, language),
        "answer": resolution_text,
        "options": _decision_options(decision),
        "action_key": action_key,
        "resolved_by": str(actor_id),
        "resolved_at": now.isoformat(),
        "verified": False,
        # A human-legible summary the settle sink uses as the garden note body /
        # title. Capped so a long answer can't blow up the note size.
        "summary": (
            f"Decision resolved — Q: {_question_text(decision, language)} A: {resolution_text}"
        )[:_SUMMARY_CAP],
        **await settle_run_context(session, run),
    }
    session.add(
        ExecutionRunActivity(
            id=uuid.uuid4(),
            run_id=run.id,
            workspace_id=run.workspace_id,
            activity_type="settle",
            payload=settle_payload,
        )
    )

    # G1 — when the founder DISCARDS with a reason, capture that rejection as
    # reusable negative knowledge. Emit a SECOND settle activity (additive to the
    # decision_resolution row above) carrying ``kind = negative_pattern`` + the
    # reason + the run's stable clustering context. The same SettleWorker drains
    # it into the vault; the NegativePatternRetriever then surfaces it as "avoid
    # this" guidance for a future run with overlapping signals — so the rejected
    # approach is not repeated. Gated on a non-empty reason: a reasonless discard
    # teaches nothing, so it writes no negative-pattern row.
    reason_text = reason.strip()
    if action_key == ACTION_DISCARD and reason_text:
        negative_payload: dict[str, Any] = {
            "kind": NEGATIVE_PATTERN_SETTLE_KIND,
            "decision_id": str(decision.id),
            "question": _question_text(decision, language),
            "reason": reason_text,
            "resolved_by": str(actor_id),
            "resolved_at": now.isoformat(),
            # A rejection is an honest founder signal, NEVER verified-as-code.
            "verified": False,
            "summary": (f"Rejected approach — {reason_text}")[:_SUMMARY_CAP],
            **await settle_run_context(session, run),
        }
        session.add(
            ExecutionRunActivity(
                id=uuid.uuid4(),
                run_id=run.id,
                workspace_id=run.workspace_id,
                activity_type="settle",
                payload=negative_payload,
            )
        )

    await session.flush()

    # L-D2: action-driven resolutions dispatch to side-effecting handlers
    # (``ship`` → promote run to shipped + create deliverable; ``discard`` →
    # abandon run). Free-text resolutions keep the prior resume-loop semantics
    # (RUNNING → OPEN). The dispatch table is keyed solely on ``action_key`` —
    # the same handler serves every Decision kind that opts into that action.
    from backend.workflow.application.agent_runner import AgentRunner  # noqa: PLC0415

    runner = AgentRunner(session)
    if action_key == ACTION_SHIP:
        await _ship_decision_run(
            session, runner, run=run, decision=decision, deliverables=deliverables
        )
    elif action_key == ACTION_DISCARD:
        await _discard_decision_run(runner, run=run, decision=decision)
    else:
        # Resume: RUNNING → OPEN so AgentWorker.drive_once (scans OPEN runs)
        # re-picks it. AgentRunner.transition no-ops if the run is not RUNNING
        # (e.g. already OPEN), harmless — the answer is recorded + folded in.
        await runner.transition(
            run_id=run.id,
            to_status=RunStatus.OPEN,
            reason=f"resumed: decision {decision.id} resolved",
        )

    # B15 — emit ``DecisionResolved`` onto the audit outbox so the supervisor
    # audit stream sees the founder's resolution (alongside the settle activity
    # row above). Soft-fail via :func:`safe_emit`. The actor is the founder
    # (``type="user"`` — this is a human action, NOT a system event like the
    # loop-side ``DecisionPending``).
    await safe_emit(
        DecisionResolved(
            actor=AuditActor(type="user", id=str(actor_id)),
            workspace_id=str(workspace_id),
            resource=AuditResource(type="execution_run", id=str(run.id)),
            data={
                "run_id": str(run.id),
                "decision_id": str(decision.id),
                "kind": decision.decision,
                "answer": resolution_text[:500],
                "action_key": action_key,
            },
        ),
        session=session,
    )

    return CheckpointResolutionOutcome(
        decision_id=decision.id,
        run_id=run.id,
        status=decision.status,
        resolution=resolution_text,
        resolved_at=now,
        run_status=run.status,
    )


async def _ship_decision_run(
    session: AsyncSession,
    runner: AgentRunner,
    *,
    run: ExecutionRun,
    decision: Decision,
    deliverables: DeliverableRepository,
) -> None:
    """L-D2 ``ship`` handler — founder overrides verification.

    1. Take the run's latest :class:`WorkStep` and mark it
       ``VERIFIED`` / ``ProofState.VERIFIED`` — a founder override,
       distinct from a real-passing verifier (the audit row records the
       human resolution; B4 trust integrity is preserved because the
       Decision carries the kind = "verification_failed" or
       "human_review_required" the founder consciously approved past).
    2. Create a code :class:`Deliverable` from the artifact_refs recorded
       on the Decision payload at mint time (if any). ``payload.shipped_by_founder``
       carries the override flag for the downstream delivery dispatcher.
    3. Transition the run RUNNING → REVIEW_READY → SHIPPED in two hops
       (state-machine valid path; the worker would have made the first
       hop on a verifier PASS, the founder makes both here in one click).
    """
    payload = decision.payload if isinstance(decision.payload, dict) else {}
    artifact_refs_raw = payload.get("artifact_refs")
    artifact_refs: list[str] = (
        [r for r in artifact_refs_raw if isinstance(r, str)]
        if isinstance(artifact_refs_raw, list)
        else []
    )

    work_step = (
        (
            await session.execute(
                select(WorkStep)
                .where(WorkStep.run_id == run.id)
                .order_by(WorkStep.created_at.desc())
            )
        )
        .scalars()
        .first()
    )
    if work_step is not None:
        work_step.status = WorkStepStatus.VERIFIED
        # DB-level ProofState enum is {UNTESTED, PROVED, REFUTED} — PROVED is
        # the success terminal (not "VERIFIED", which is a WorkStepStatus). A
        # founder ship promotes the proof to PROVED with the Decision audit
        # row recording the override (B4 trust integrity).
        work_step.proof_state = ProofState.PROVED

    # L-P2: ship_or_discard Decisions are minted on REVIEW_READY runs that
    # ALREADY have a Deliverable from the verifier's PASS path. Don't mint
    # a duplicate — the existing row already carries the verified artifact
    # refs. For verification_failed / human_review_required Decisions there
    # is no prior Deliverable, so the mint below is the first one.
    existing_deliverable = await deliverables.find_first_by_run(run.id)
    if existing_deliverable is None:
        await deliverables.add(
            Deliverable(
                id=uuid.uuid4(),
                run_id=run.id,
                workspace_id=run.workspace_id,
                deliverable_type=DeliverableType.CODE,
                payload={
                    "shipped_by_founder": True,
                    "decision_id": str(decision.id),
                    "artifact_refs": artifact_refs,
                },
            )
        )

    # W2 — when the run is bound to a product workspace, ship_anyway means
    # "force the run's version onto main" (the founder explicitly accepted
    # the work even though verify failed). git -X theirs is the strategy
    # that says "on conflict, take the merging-in branch's content"; the
    # branch being merged in is the run branch, so the run wins.
    if run.product_id is not None:
        from backend.storage.product_workspace import (  # noqa: PLC0415
            ProductWorkspaceBusy as _StorageProductWorkspaceBusy,
        )
        from backend.storage.product_workspace import (  # noqa: PLC0415
            ProductWorkspaceError,
            commit_worktree,
            force_merge_theirs,
            product_workspace_lock,
            remove_run_worktree,
        )

        try:
            # The agent may not have committed its uncommitted work yet
            # (verification failed before the verify hook called
            # commit_worktree). Force a commit now so force_merge_theirs
            # has a branch to merge from.
            await commit_worktree(
                run.product_id,
                run.id,
                message=f"ship_anyway: decision {decision.id}",
            )
            async with product_workspace_lock(session, run.product_id):
                await force_merge_theirs(run.product_id, run.id)
            try:
                await remove_run_worktree(run.product_id, run.id)
            except ProductWorkspaceError:
                logger.warning(
                    "ship_anyway_worktree_cleanup_failed",
                    run_id=str(run.id),
                    exc_info=True,
                )
        except _StorageProductWorkspaceBusy:
            logger.warning("ship_anyway_lock_busy", run_id=str(run.id))
            # Surface as HTTP 503-ish via the resolve service's exception path —
            # let the founder retry. v1 keeps it simple.
            raise ProductWorkspaceBusy("product workspace busy; retry in a moment") from None
        except ProductWorkspaceError as exc:
            logger.warning("ship_anyway_merge_failed", run_id=str(run.id), exc_info=True)
            raise ProductWorkspaceMergeFailed(f"ship_anyway merge failed: {exc}") from exc

    await runner.transition(
        run_id=run.id,
        to_status=RunStatus.REVIEW_READY,
        reason=f"founder approve+ship via decision {decision.id}",
    )
    await runner.transition(
        run_id=run.id,
        to_status=RunStatus.SHIPPED,
        reason=f"founder approve+ship via decision {decision.id}",
    )


async def _discard_decision_run(
    runner: AgentRunner,
    *,
    run: ExecutionRun,
    decision: Decision,
) -> None:
    """L-D2 ``discard`` handler — founder abandons the run.

    No deliverable is created and no proof override happens. The run goes
    straight to ABANDONED; any WorkStep already in a non-terminal state is
    moot since the abandonment is the terminal signal for the whole run.

    W2 — when the run is bound to a product workspace, also clean up the
    worktree + branch so the founder doesn't see a "ghost" branch in
    ``git branch`` later.
    """
    # RunStatus enum has no ABANDONED — CANCELLED is the discard terminal.
    await runner.transition(
        run_id=run.id,
        to_status=RunStatus.CANCELLED,
        reason=f"founder discard via decision {decision.id}",
    )

    if run.product_id is not None:
        from backend.storage.product_workspace import (  # noqa: PLC0415
            ProductWorkspaceError,
            remove_run_worktree,
        )

        try:
            await remove_run_worktree(run.product_id, run.id)
        except ProductWorkspaceError:
            logger.warning(
                "discard_worktree_cleanup_failed",
                run_id=str(run.id),
                exc_info=True,
            )


__all__ = [
    "DECISION_RESOLUTION_SETTLE_KIND",
    "NEGATIVE_PATTERN_SETTLE_KIND",
    "CheckpointNotFound",
    "CheckpointResolutionError",
    "CheckpointResolutionOutcome",
    "InvalidAction",
    "ProductWorkspaceBusy",
    "ProductWorkspaceMergeFailed",
    "resolve_checkpoint",
]
