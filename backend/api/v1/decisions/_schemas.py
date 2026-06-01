"""Shared Pydantic schemas for ``/api/v1/decisions`` (Lift M1).

The list (read) and resolve (accept/reject) sub-files both surface ``status``
strings + proposal/decision payloads — extracted here so each endpoint file
stays a thin adapter.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class ProposalResponse(BaseModel):
    """One queued proposal, sourced from the workspace vault.

    ``id`` is the proposal's vault path — the natural handle the resolution
    endpoints address (``POST /api/v1/decisions/{proposal_id:path}/accept``),
    so a listed proposal round-trips straight into accept/reject.
    ``action_kind`` / ``action_path`` are derived from the proposal's first
    linked action draft (``actions/<kind>/...``): the human-readable handle for
    what approving the proposal would mutate. Field set mirrors the previous
    (DB-sourced) response so existing consumers + the PWA contract are
    unchanged.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    proposal_kind: str
    action_kind: str
    action_path: str
    status: str
    score: float | None = None
    created_at: datetime
    expires_at: datetime | None = None


class DecisionResponse(BaseModel):
    """One resolved decision-memory note (founder-approval audit trail).

    Sourced from the vault ``decisions/<kind>/...`` notes. ``id`` is the
    decision's vault path; ``decision_kind`` is the directional decision
    (``cannot-link`` / ``must-link``).
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    proposal_id: str | None = None
    decision_kind: str
    actor_id: str | None = None
    created_at: datetime


class ApplyResultResponse(BaseModel):
    """One linked action's apply outcome (mirror of ``models.ApplyResult``)."""

    model_config = ConfigDict(extra="forbid")

    action_path: str
    final_status: str
    affected_paths: list[str]
    error: str | None = None


class AcceptResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposal_path: str
    status: str
    results: list[ApplyResultResponse]


class RejectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str | None = None


class RejectResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposal_path: str
    status: str
    reason: str | None = None


__all__ = [
    "AcceptResponse",
    "ApplyResultResponse",
    "DecisionResponse",
    "ProposalResponse",
    "RejectRequest",
    "RejectResponse",
]
