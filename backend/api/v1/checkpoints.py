"""/api/v1/checkpoints — founder resolution of paused-run Decisions.

Workflow §5 #4 / §12.5 #8. When the agent loop is stuck or the work LLM
calls ``ask_user_question``, :class:`~backend.execution.orchestrator.RunOrchestrator`
mints an ``execution_decisions`` row and the run pauses (stays RUNNING — not a
DB terminal). This router is the founder's re-entry point:

* ``GET  /api/v1/checkpoints`` — list PENDING execution Decisions for the
  workspace (the blocking questions awaiting a human answer).
* ``POST /api/v1/checkpoints/{id}/resolve`` — record the founder's answer on
  the Decision, fold it into the run payload, and resume the paused run by
  transitioning it RUNNING → OPEN so :meth:`AgentWorker.drive_once` (which
  scans ``status==OPEN`` runs) re-picks it. The orchestrator then injects the
  resolved answer into the loop's initial messages so the work continues with
  the founder's decision in context.

v1 resumes the run *inline* here (not via the event-driven
:class:`~backend.intake.decision_resolution.DecisionResolutionTrigger`, which
remains a future option). This is simpler — no phantom Request, the paused run
is resumed directly.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import get_current_user_row, get_db_session, get_workspace_id
from backend.execution.audit_events import DecisionResolved
from backend.execution.db import (
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
from backend.execution.verified_deliverable import settle_run_context
from backend.identity.db import UserRow
from backend.orchestrator.agent_runner import AgentRunner
from backend.supervisor.audit.events import AuditActor, AuditResource
from backend.supervisor.audit.service import safe_emit

#: Payload ``kind`` on the settle activity emitted by the resolve endpoint
#: (B11b). The :class:`~backend.workers.settle_worker.SettleWorker` drains the
#: row into the workspace's BSage vault — turning the answered Decision into
#: reusable knowledge so a future run with similar signals doesn't re-ask the
#: same question. The kind is stable wire shape; downstream consumers
#: (retriever, audit) key off it.
DECISION_RESOLUTION_SETTLE_KIND = "decision_resolution"

#: Cap on the settle-activity ``summary`` text — keeps the absorbed garden
#: note's body proportionate to the question + answer (mirrors
#: :data:`~backend.execution.verified_deliverable._SETTLE_SUMMARY_CAP`).
_SUMMARY_CAP = 500

router = APIRouter()


class DecisionAction(BaseModel):
    """L-D2: a one-click action available on an executor B2b Decision.

    The founder clicks the action (PWA renders a dedicated button) instead of
    typing a free-text resolution; the resolve endpoint dispatches on ``key``
    to a side-effecting handler (e.g. ``ship`` promotes the run to shipped +
    creates the deliverable; ``discard`` abandons it). Labels are sent for
    every supported locale so the PWA renders them client-side without a
    per-product i18n lookup."""

    model_config = ConfigDict(extra="forbid")

    key: str
    label_en: str
    label_ko: str


class CheckpointResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    run_id: uuid.UUID
    decision: str
    question: str
    # B11a: structured options the work LLM offered (``ask_user_question`` with
    # an ``options`` array). The PWA renders a single-select; the founder may
    # pick one OR write an "Other" free-text answer (L-D1). ``None`` (or empty)
    # keeps the existing pure-textarea behaviour.
    options: list[str] | None = None
    # L-D2: structured one-click actions for executor B2b Decisions
    # (``verification_failed`` / ``human_review_required``). ``None`` for
    # ask_user_question Decisions — those use ``options`` + free-text only.
    actions: list[DecisionAction] | None = None
    rationale: str | None = None
    created_at: datetime


class ResolvedCheckpointResponse(BaseModel):
    """One answered paused-run checkpoint (the Decisions "Resolved" tab,
    checkpoint side): the question + the founder's recorded answer + when."""

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    run_id: uuid.UUID
    question: str
    resolution: str | None = None
    resolved_at: datetime | None = None


class ResolveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Either ``action_key`` (L-D2 one-click) OR ``answer`` (free-text /
    # L-D1 options + Other). When both are sent, ``action_key`` wins —
    # the dispatch-driven path is the authoritative one. ``answer`` is
    # optional now (was required) because action-only resolutions don't
    # carry founder text; defaults to ``""`` and the handler resolves
    # the recorded ``decision.resolution`` from the action key instead.
    answer: str = ""
    action_key: str | None = None


class ResolveResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    run_id: uuid.UUID
    status: DecisionStatus
    resolution: str
    resolved_at: datetime
    run_status: RunStatus


# B4: executor B2b Decisions (raised when an executor run does NOT verify) record
# ``payload.reason`` instead of ``payload.question`` — they are an honest "this
# needs you" surfaced as a Decision, not a work-LLM question. Map the kind →
# a calm, human-readable line so the founder never sees a blank question on a
# genuinely actionable needs-you item.
_EXECUTOR_DECISION_QUESTIONS: dict[str, str] = {
    "verification_failed": "BSVibe couldn't verify this work — review it before it ships?",
    "human_review_required": "This work needs your review before BSVibe can call it verified.",
}


# L-D2: per-kind action specs surfaced on every executor B2b Decision the
# founder can act on with one click. Labels ship for every supported locale
# so the PWA can render them without an extra round-trip. Action ``key``s
# are stable wire identifiers — handlers dispatch on them in
# :func:`resolve_checkpoint`. Adding a new action = one entry here + one
# handler. New Decision kinds may opt in by adding themselves to this map.
ACTION_SHIP = "ship"
ACTION_DISCARD = "discard"

_EXECUTOR_DECISION_ACTIONS: dict[str, list[DecisionAction]] = {
    "verification_failed": [
        DecisionAction(key=ACTION_SHIP, label_en="Approve & ship", label_ko="승인하고 출시"),
        DecisionAction(key=ACTION_DISCARD, label_en="Discard", label_ko="폐기"),
    ],
    "human_review_required": [
        DecisionAction(key=ACTION_SHIP, label_en="Approve & ship", label_ko="승인하고 출시"),
        DecisionAction(key=ACTION_DISCARD, label_en="Discard", label_ko="폐기"),
    ],
}


def _decision_actions(decision: Decision) -> list[DecisionAction] | None:
    """The structured one-click actions for ``decision``, or ``None`` if the
    kind doesn't carry any (a vanilla ask_user_question Decision)."""
    return _EXECUTOR_DECISION_ACTIONS.get(decision.decision)


def _question_text(decision: Decision) -> str:
    """The founder-facing question for a paused-run Decision.

    Prefers the work LLM's recorded ``payload.question`` (the ``ask_user_question``
    path). For an executor B2b Decision — which records ``payload.reason``, not a
    question — fall back to a calm kind-derived line so the needs-you item is
    never blank. A wholly unrecognised reason-only Decision degrades to an empty
    string (unchanged), never raising."""
    payload = decision.payload or {}
    if isinstance(payload, dict):
        value = payload.get("question")
        if isinstance(value, str) and value.strip():
            return value
    fallback = _EXECUTOR_DECISION_QUESTIONS.get(decision.decision)
    return fallback if fallback is not None else ""


def _decision_options(decision: Decision) -> list[str] | None:
    """The structured options offered for this paused-run Decision, if any.

    B11a: the work LLM's ``ask_user_question`` may carry an ``options`` array on
    the Decision payload. Only return a clean list of non-empty strings; any
    other shape degrades to ``None`` so the PWA falls back to free-text and the
    resolve endpoint skips the membership check (existing behaviour)."""
    payload = decision.payload or {}
    if not isinstance(payload, dict):
        return None
    raw = payload.get("options")
    if not isinstance(raw, list):
        return None
    cleaned = [item for item in raw if isinstance(item, str) and item.strip()]
    return cleaned or None


@router.get("")
async def list_checkpoints(
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> list[CheckpointResponse]:
    """List PENDING execution Decisions for the workspace, newest first."""
    stmt = (
        select(Decision)
        .where(
            Decision.workspace_id == workspace_id,
            Decision.status == DecisionStatus.PENDING,
        )
        .order_by(Decision.created_at.desc())
    )
    rows = (await session.execute(stmt)).scalars().all()
    return [
        CheckpointResponse(
            id=row.id,
            run_id=row.run_id,
            decision=row.decision,
            question=_question_text(row),
            options=_decision_options(row),
            actions=_decision_actions(row),
            rationale=row.rationale,
            created_at=row.created_at,
        )
        for row in rows
    ]


@router.get("/resolved")
async def list_resolved_checkpoints(
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> list[ResolvedCheckpointResponse]:
    """List RESOLVED execution Decisions for the Decisions "Resolved" tab,
    most-recently-resolved first (created_at as a stable tiebreaker)."""
    stmt = (
        select(Decision)
        .where(
            Decision.workspace_id == workspace_id,
            Decision.status == DecisionStatus.RESOLVED,
        )
        .order_by(Decision.resolved_at.desc(), Decision.created_at.desc())
    )
    rows = (await session.execute(stmt)).scalars().all()
    return [
        ResolvedCheckpointResponse(
            id=row.id,
            run_id=row.run_id,
            question=_question_text(row),
            resolution=row.resolution,
            resolved_at=row.resolved_at,
        )
        for row in rows
    ]


@router.post("/{checkpoint_id}/resolve")
async def resolve_checkpoint(
    checkpoint_id: uuid.UUID,
    body: ResolveRequest,
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    user_row: Annotated[UserRow, Depends(get_current_user_row)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> ResolveResponse:
    """Resolve a pending Decision with the founder's answer and resume the run.

    404 when the Decision is not in the caller's workspace or is not pending.
    On success: record the answer on the Decision, append it to the run's
    ``payload["resolved_decisions"]``, and transition the run RUNNING → OPEN so
    the worker re-picks it (the loop then sees the answer in its messages).
    """
    decision = await session.get(Decision, checkpoint_id)
    if (
        decision is None
        or decision.workspace_id != workspace_id
        or decision.status is not DecisionStatus.PENDING
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Pending checkpoint {checkpoint_id} not found",
        )

    # L-D2: validate ``action_key`` against the Decision kind's allowlist BEFORE
    # any side effects. An unknown key → 400 (the Decision stays pending). An
    # action key on a Decision that has no actions → 400.
    available_actions = _decision_actions(decision)
    action_key = body.action_key
    if action_key is not None:
        if available_actions is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Decision kind {decision.decision!r} has no one-click actions",
            )
        if action_key not in {a.key for a in available_actions}:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"action_key {action_key!r} is not allowed for {decision.decision!r}",
            )
    elif not body.answer.strip():
        # Free-text path requires a non-empty answer (was previously enforced
        # via Pydantic min_length=1; relaxed because action-only resolves
        # skip ``answer``).
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="answer must be non-empty when no action_key is given",
        )

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
    resolution_text = action_key if action_key is not None else body.answer
    decision.status = DecisionStatus.RESOLVED
    decision.resolution = resolution_text
    decision.resolved_at = now
    decision.resolved_by = user_row.id

    run = await session.get(ExecutionRun, decision.run_id)
    if run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Run {decision.run_id} for checkpoint not found",
        )

    # Fold the resolution into the run payload so the loop seeds it as context.
    # Re-assign payload (not in-place mutate) so SQLAlchemy detects the change
    # on a JSON column.
    payload: dict[str, Any] = dict(run.payload or {})
    resolved = list(payload.get("resolved_decisions") or [])
    resolved.append(
        {
            "decision_id": str(decision.id),
            "question": _question_text(decision),
            "answer": resolution_text,
        }
    )
    payload["resolved_decisions"] = resolved
    run.payload = payload

    await session.flush()

    # B11b — Knowledge-ize the resolution. Emit a ``settle`` ExecutionRunActivity
    # carrying the decision-resolution payload + the run's stable clustering
    # context (intent/product). The :class:`~backend.workers.settle_worker.SettleWorker`
    # drains this row into the workspace's BSage vault, exactly like a
    # verified-work observation — so a future run with similar signals can
    # surface the prior decision via the retriever (the SAME seam B3 verify
    # and B6 seed inject). ``verified`` is False — the resolution is an honest
    # answer, NOT verified-as-code (B4 trust integrity).
    settle_payload: dict[str, Any] = {
        "kind": DECISION_RESOLUTION_SETTLE_KIND,
        "decision_id": str(decision.id),
        "question": _question_text(decision),
        "answer": resolution_text,
        "options": _decision_options(decision),
        "action_key": action_key,
        "resolved_by": str(user_row.id),
        "resolved_at": now.isoformat(),
        "verified": False,
        # A human-legible summary the settle sink uses as the garden note body /
        # title. Capped so a long answer can't blow up the note size.
        "summary": (f"Decision resolved — Q: {_question_text(decision)} A: {resolution_text}")[
            :_SUMMARY_CAP
        ],
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
    await session.flush()

    # L-D2: action-driven resolutions dispatch to side-effecting handlers
    # (``ship`` → promote run to shipped + create deliverable; ``discard`` →
    # abandon run). Free-text resolutions keep the prior resume-loop semantics
    # (RUNNING → OPEN). The dispatch table is keyed solely on ``action_key`` —
    # the same handler serves every Decision kind that opts into that action.
    runner = AgentRunner(session)
    if action_key == ACTION_SHIP:
        await _ship_decision_run(session, runner, run=run, decision=decision)
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
            actor=AuditActor(type="user", id=str(user_row.id)),
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

    await session.commit()

    return ResolveResponse(
        id=decision.id,
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

    session.add(
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
    """
    # RunStatus enum has no ABANDONED — CANCELLED is the discard terminal.
    await runner.transition(
        run_id=run.id,
        to_status=RunStatus.CANCELLED,
        reason=f"founder discard via decision {decision.id}",
    )
