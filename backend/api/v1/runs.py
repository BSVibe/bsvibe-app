"""/api/v1/runs — read API for ExecutionRun rows.

Read-only on the HTTP surface; runs are *created* by the agent loop / workers
(Bundle G), never directly by an HTTP POST.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import get_db_session, get_workspace_id
from backend.execution.db import (
    Decision,
    DecisionStatus,
    Deliverable,
    ExecutionRun,
    ExecutionRunActivity,
    RunStatus,
    VerificationOutcome,
    VerificationResult,
)

router = APIRouter()


class RunResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    workspace_id: uuid.UUID
    product_id: uuid.UUID | None = None
    request_id: uuid.UUID | None = None
    status: RunStatus
    created_at: datetime
    updated_at: datetime


class RunTriggerContext(BaseModel):
    """The "outside" that asked for this run, pulled defensively out of the
    run's free-form ``payload``.

    Connector-inbound runs carry a ``TriggerEvent(source=<connector>,
    trigger_kind=webhook)``; the payload may also carry the founder's Direction
    (``intent_text`` / ``text``) and a product slug. Each key is surfaced only
    when present AND a non-empty string — an odd value (number, list) degrades
    to ``None`` so a sparse / malformed payload never 500s the response model.
    """

    model_config = ConfigDict(extra="forbid")

    source: str | None = None
    trigger_kind: str | None = None
    intent_text: str | None = None
    product: str | None = None


class RunDecision(BaseModel):
    """One paused-run Decision: the blocking question + its resolution state.

    The founder resolves it via ``POST /api/v1/checkpoints/{id}/resolve`` (the
    run-detail UI links a PENDING decision to that re-entry point — it does not
    reinvent resolution).
    """

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    decision: str
    question: str
    rationale: str | None = None
    status: DecisionStatus
    resolution: str | None = None
    created_at: datetime


class RunVerification(BaseModel):
    """The latest VerificationResult outcome for the run, if any."""

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    outcome: VerificationOutcome
    created_at: datetime


class RunActivity(BaseModel):
    """One meaningful event on the run's timeline — the STORY of what the agent
    did ("What I did"). The ``type`` is the raw :class:`ExecutionRunActivity`
    ``activity_type`` (``tool_call`` / ``verify`` / ``settle`` / ``error``) or a
    synthesized ``deliver`` when the timeline is derived from rows we already
    carry; the ``label`` is a short human summary built defensively from the
    payload (so a malformed payload never 500s the response model)."""

    model_config = ConfigDict(extra="forbid")

    type: str
    label: str
    created_at: datetime


class RunDetailResponse(BaseModel):
    """The inspectable run-detail surface (Stitch "Triggered"): the run's
    status + timestamps, its trigger context, its paused-run Decisions, the
    latest verification outcome, the resulting Deliverable id (so the UI can
    link to its Delivery Report), and the run's activity timeline (the STORY of
    what the agent did, time-ordered oldest-first).

    ``timeline_source`` is ``"activities"`` when real
    :class:`ExecutionRunActivity` rows drive the timeline, or ``"derived"`` when
    no activity rows exist and the timeline is synthesized from the deliverable +
    verification we already carry (the DEFER fallback — only what the schema
    actually stores; no fabricated per-step LLM token traces)."""

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    workspace_id: uuid.UUID
    product_id: uuid.UUID | None = None
    status: RunStatus
    created_at: datetime
    updated_at: datetime
    trigger: RunTriggerContext
    decisions: list[RunDecision] = []
    verification: RunVerification | None = None
    deliverable_id: uuid.UUID | None = None
    activities: list[RunActivity] = []
    timeline_source: str = "derived"


def _opt_str(value: Any) -> str | None:
    """A non-empty string value, else ``None`` (tolerant of odd payload types)."""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _trigger_context(payload: Any) -> RunTriggerContext:
    """Map the free-form run payload onto the trigger-context fields, defensively."""
    payload = payload if isinstance(payload, dict) else {}
    # The founder's Direction lives under ``intent_text`` (intake) or ``text``
    # (direct submission) — fall back across both.
    intent = _opt_str(payload.get("intent_text")) or _opt_str(payload.get("text"))
    return RunTriggerContext(
        source=_opt_str(payload.get("source")),
        trigger_kind=_opt_str(payload.get("trigger_kind")),
        intent_text=intent,
        product=_opt_str(payload.get("product")),
    )


def _question_text(decision: Decision) -> str:
    payload = decision.payload or {}
    if isinstance(payload, dict):
        value = payload.get("question")
        if isinstance(value, str):
            return value
    return ""


def _write_paths(payload: dict[str, Any]) -> list[str]:
    """The file paths a ``tool_call`` activity wrote, defensively (an odd
    ``writes`` value yields an empty list rather than throwing)."""
    writes = payload.get("writes")
    if not isinstance(writes, list):
        return []
    return [p for p in writes if isinstance(p, str) and p.strip()]


_VERIFY_LABELS = {
    "passed": "Verified the work",
    "failed": "Verification failed",
    "inconclusive": "Verification was inconclusive",
}


def _tool_call_label(payload: dict[str, Any]) -> str | None:
    """ "Delivered X" for a file-writing tool_call; ``None`` for a read-only one
    (noise the founder timeline skips)."""
    paths = _write_paths(payload)
    if not paths:
        return None
    shown = ", ".join(paths[:3])
    if len(paths) > 3:
        shown += f" (+{len(paths) - 3} more)"
    return f"Delivered {shown}"


def _activity_label(activity_type: str, payload: dict[str, Any]) -> str | None:
    """A short human label for one ExecutionRunActivity, or ``None`` when the
    event is low-signal noise the founder timeline should skip.

    Surfaced events tell the run's STORY: a file-writing ``tool_call``
    ("Delivered X"), a ``verify`` verdict, a ``settle`` ("Settled into
    knowledge"), and a calm ``error``. Per-turn ``llm_turn`` chatter and
    read-only ``tool_call`` rows are noise and drop out (→ ``None``). All payload
    reads are defensive so a malformed row degrades to a calm label / drop rather
    than 500ing the response model.
    """
    if activity_type == "tool_call":
        return _tool_call_label(payload)
    if activity_type == "verify":
        outcome = payload.get("outcome")
        if isinstance(outcome, str):
            return _VERIFY_LABELS.get(outcome, "Ran verification")
        return "Ran verification"
    if activity_type == "settle":
        return "Settled into knowledge"
    if activity_type == "error":
        return "Hit a problem"
    # llm_turn and any unknown / low-signal type are skipped.
    return None


def _build_timeline(
    activity_rows: list[ExecutionRunActivity],
    verification: VerificationResult | None,
    deliverable_id: uuid.UUID | None,
    deliverable_created_at: datetime | None,
) -> tuple[list[RunActivity], str]:
    """Build the run's STORY timeline (oldest-first) + its source tag.

    Prefers REAL :class:`ExecutionRunActivity` rows (``timeline_source ==
    "activities"``). When none exist, DERIVES a calm timeline from the rows we
    already carry — the latest verification + the resulting deliverable (the
    DEFER fallback; ``timeline_source == "derived"``). Surfaces only what the
    schema actually stores — no fabricated per-step token traces.
    """
    if activity_rows:
        events: list[RunActivity] = []
        for row in activity_rows:
            payload = row.payload if isinstance(row.payload, dict) else {}
            label = _activity_label(row.activity_type, payload)
            if label is None:
                continue
            events.append(
                RunActivity(type=row.activity_type, label=label, created_at=row.created_at)
            )
        return events, "activities"

    # Derived fallback: synthesize from the verification + deliverable we have.
    derived: list[RunActivity] = []
    if verification is not None:
        label = _activity_label("verify", {"outcome": verification.outcome.value})
        if label is not None:
            derived.append(
                RunActivity(type="verify", label=label, created_at=verification.created_at)
            )
    if deliverable_id is not None and deliverable_created_at is not None:
        derived.append(
            RunActivity(
                type="deliver", label="Produced a deliverable", created_at=deliverable_created_at
            )
        )
    derived.sort(key=lambda e: e.created_at)
    return derived, "derived"


@router.get("")
async def list_runs(
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    limit: int = 50,
) -> list[RunResponse]:
    """List recent ExecutionRun rows for the workspace, newest first."""
    limit = max(1, min(limit, 200))
    stmt = (
        select(ExecutionRun)
        .where(ExecutionRun.workspace_id == workspace_id)
        .order_by(ExecutionRun.created_at.desc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    rows = result.scalars().all()
    return [
        RunResponse(
            id=row.id,
            workspace_id=row.workspace_id,
            product_id=row.product_id,
            request_id=row.request_id,
            status=row.status,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )
        for row in rows
    ]


@router.get("/{run_id}")
async def get_run(
    run_id: uuid.UUID,
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> RunResponse:
    """Fetch one ExecutionRun by id, scoped to the caller's workspace."""
    row = await session.get(ExecutionRun, run_id)
    if row is None or row.workspace_id != workspace_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Run {run_id} not found")
    return RunResponse(
        id=row.id,
        workspace_id=row.workspace_id,
        product_id=row.product_id,
        request_id=row.request_id,
        status=row.status,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@router.get("/{run_id}/detail")
async def get_run_detail(
    run_id: uuid.UUID,
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> RunDetailResponse:
    """The inspectable run-detail surface for one ExecutionRun (Stitch
    "Triggered"), scoped to the caller's workspace.

    Bundles the run's trigger context (defensively read out of the free-form
    ``payload``), its paused-run Decisions (the blocking questions the founder
    resolves via /api/v1/checkpoints), the latest VerificationResult outcome,
    and the resulting Deliverable id (so the UI can link to its Delivery
    Report). A cross-workspace / unknown id is 404, never a leak; a run with a
    sparse payload degrades to a calm minimal detail rather than erroring.
    """
    run = await session.get(ExecutionRun, run_id)
    if run is None or run.workspace_id != workspace_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Run {run_id} not found")

    decisions_stmt = (
        select(Decision)
        .where(Decision.run_id == run_id, Decision.workspace_id == workspace_id)
        .order_by(Decision.created_at.desc())
    )
    decision_rows = (await session.execute(decisions_stmt)).scalars().all()

    latest_verification_stmt = (
        select(VerificationResult)
        .where(
            VerificationResult.run_id == run_id,
            VerificationResult.workspace_id == workspace_id,
        )
        .order_by(VerificationResult.created_at.desc())
        .limit(1)
    )
    verification_row = (await session.execute(latest_verification_stmt)).scalars().first()

    latest_deliverable_stmt = (
        select(Deliverable.id, Deliverable.created_at)
        .where(
            Deliverable.run_id == run_id,
            Deliverable.workspace_id == workspace_id,
        )
        .order_by(Deliverable.created_at.desc())
        .limit(1)
    )
    deliverable_row = (await session.execute(latest_deliverable_stmt)).first()
    deliverable_id = deliverable_row[0] if deliverable_row is not None else None
    deliverable_created_at = deliverable_row[1] if deliverable_row is not None else None

    # The run's STORY: meaningful activity rows, oldest-first.
    activities_stmt = (
        select(ExecutionRunActivity)
        .where(
            ExecutionRunActivity.run_id == run_id,
            ExecutionRunActivity.workspace_id == workspace_id,
        )
        .order_by(ExecutionRunActivity.created_at.asc())
    )
    activity_rows = list((await session.execute(activities_stmt)).scalars().all())
    activities, timeline_source = _build_timeline(
        activity_rows, verification_row, deliverable_id, deliverable_created_at
    )

    return RunDetailResponse(
        id=run.id,
        workspace_id=run.workspace_id,
        product_id=run.product_id,
        status=run.status,
        created_at=run.created_at,
        updated_at=run.updated_at,
        trigger=_trigger_context(run.payload),
        decisions=[
            RunDecision(
                id=d.id,
                decision=d.decision,
                question=_question_text(d),
                rationale=d.rationale,
                status=d.status,
                resolution=d.resolution,
                created_at=d.created_at,
            )
            for d in decision_rows
        ],
        verification=(
            RunVerification(
                id=verification_row.id,
                outcome=verification_row.outcome,
                created_at=verification_row.created_at,
            )
            if verification_row is not None
            else None
        ),
        deliverable_id=deliverable_id,
        activities=activities,
        timeline_source=timeline_source,
    )
