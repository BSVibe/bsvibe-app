"""Shared Pydantic schemas for the ``/api/v1/runs`` surface (Lift M1).

Used by :mod:`.list_get` (the list / single-row read) and :mod:`.detail`
(the inspectable run-detail view) — split into a shared module so the two
endpoint files can stay D35-thin without duplicating the response shapes.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from backend.workflow.infrastructure.db import (
    DecisionStatus,
    RunStatus,
    VerificationOutcome,
)


class RunResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    workspace_id: uuid.UUID
    product_id: uuid.UUID | None = None
    request_id: uuid.UUID | None = None
    status: RunStatus
    # The founder's Direction for this run (from the free-form payload's
    # ``intent_text`` / ``text``); ``None`` when the run carries none. Powers the
    # "what is BSVibe working on" title on the merged Brief / Work-Home surface.
    intent: str | None = None
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


class RunPartialDeliverable(BaseModel):
    """D6 — one mid-loop partial Deliverable (Synthesis §13 / Workflow §1).

    Distinct from the verified-final Deliverable: each one is a single external
    artifact the agent loop emitted via ``emit_deliverable`` BEFORE reaching
    the verified terminal (a PR, a Notion page, a comment, …). The Run-view
    renders these in a streaming list, separated from the verified-final the
    founder taps for the Delivery Report.

    Fields read defensively from the Deliverable's free-form ``payload`` — a
    sparse / malformed payload yields a calm minimal entry (id + timestamp)
    rather than 500ing the response model.
    """

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    artifact_type: str
    summary: str | None = None
    channel: str | None = None
    external_ref: str | None = None
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
    actually stores; no fabricated per-step LLM token traces).

    D6 — ``deliverable_id`` is the run's VERIFIED-FINAL Deliverable (the
    terminal CODE row written by ``write_verified_deliverable``); mid-loop
    partial Deliverables are surfaced separately in ``partial_deliverables``
    (oldest-first, the order they were emitted). A run with zero mid-loop
    emits keeps the prior shape exactly (empty list).
    """

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
    partial_deliverables: list[RunPartialDeliverable] = []
    activities: list[RunActivity] = []
    timeline_source: str = "derived"
    # L2 (#9): WHY a terminal-failed run failed — the latest
    # ExecutionRunHistory ``reason`` for a FAILED / CANCELLED transition. The
    # founder sees the cause (and a Retry affordance) instead of a blank
    # "nothing to do" dead-end. ``None`` for non-failed runs.
    failure_reason: str | None = None


class RunRetryResponse(BaseModel):
    """The result of re-opening a terminal-failed run (L2 #9): the run is back
    to ``OPEN`` so ``AgentWorker.drive_once`` re-picks it for another attempt."""

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    status: RunStatus
    retry_count: int


__all__ = [
    "RunActivity",
    "RunDecision",
    "RunDetailResponse",
    "RunPartialDeliverable",
    "RunResponse",
    "RunRetryResponse",
    "RunTriggerContext",
    "RunVerification",
]
