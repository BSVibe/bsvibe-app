"""Shared adapter helpers for the ``/api/v1/runs`` surface (Lift M1).

Defensive payload mappers + timeline builders, factored out so each endpoint
body stays a thin parse → app-service → serialize adapter (D35). The detail
endpoint uses every helper here; the list / single-row reads use ``_intent_of``.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from backend.workflow.infrastructure.db import (
    Decision,
    Deliverable,
    ExecutionRunActivity,
    VerificationResult,
)

from ._schemas import (
    RunActivity,
    RunPartialDeliverable,
    RunTriggerContext,
)


def _opt_str(value: Any) -> str | None:
    """A non-empty string value, else ``None`` (tolerant of odd payload types)."""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _intent_of(payload: Any) -> str | None:
    """The founder's Direction from a run's free-form payload (``intent_text``
    from intake, or ``text`` from a direct submission); ``None`` when neither is
    a non-empty string. Same resolution the trigger context + report use."""
    payload = payload if isinstance(payload, dict) else {}
    return _opt_str(payload.get("intent_text")) or _opt_str(payload.get("text"))


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


def _partial_deliverable(row: Deliverable) -> RunPartialDeliverable:
    """D6 — map a mid-loop partial Deliverable row onto the response shape.

    All payload reads are defensive (a non-string ``summary``, missing
    ``artifact_type``, etc. degrade to ``None`` / the raw enum value) so a
    malformed payload never 500s the response model.
    """
    payload = row.payload if isinstance(row.payload, dict) else {}
    raw_artifact_type = payload.get("artifact_type")
    artifact_type = (
        raw_artifact_type
        if isinstance(raw_artifact_type, str) and raw_artifact_type.strip()
        else row.deliverable_type.value
    )
    return RunPartialDeliverable(
        id=row.id,
        artifact_type=artifact_type,
        summary=_opt_str(payload.get("summary")),
        channel=_opt_str(payload.get("channel")),
        external_ref=_opt_str(payload.get("external_ref")),
        created_at=row.created_at,
    )


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


__all__ = [
    "_activity_label",
    "_build_timeline",
    "_intent_of",
    "_partial_deliverable",
    "_question_text",
    "_trigger_context",
]
