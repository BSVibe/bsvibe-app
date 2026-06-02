"""RetractionSignal — the typed contract for an ontology retraction / correction.

Lift M3a. The wire shape every founder-issued retraction or correction flows
through, end to end:

  PWA Inside view (founder click)
    → REST handler (RBAC, validate)
      → :class:`RetractionService.issue` (DB row + audit emit)
        → 30s undo window (DB-backed)
          → :class:`RetractionService.apply` (vault tombstone via writer)
            → :class:`ResolvedDecisionsRetriever` skip-retracted predicate
              → future runs never see this node

The signal is intentionally smaller than the full
``OntologyCorrectionSignal`` sketched in
``~/Docs/BSVibe_Ontology_Inspect_Correct_Design_2026-05-30.md`` §2.1 —
dependents-walk + correlation_run_id are deferred to a follow-up lift. The
five required + one optional fields here are the load-bearing contract:
identity (workspace + actor), target node, action, undo deadline, audit
chain. Idempotence is on ``id``: a second issue of the same correction id
on the same workspace + node is a no-op (the existing row is returned).

The optional ``reason`` field is founder-typed free text (capped at 280
chars to stay in toast-friendly territory) — design Q2 founder-confirmed
optional, low-friction.

The model is ``extra="forbid"`` (typo-resistant — a misnamed field at emit
time becomes a validation error, not a silently-dropped JSON blob), and
``frozen=True`` (a signal is the audit record; once issued, mutating its
content would invalidate the audit trail).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

#: How long a founder has after issuing a retract / correct to undo it. The
#: design (§3.4) locks this at 30 seconds — calm but quick. Stored on the
#: signal as ``apply_at = issued_at + UNDO_WINDOW_SECONDS``.
UNDO_WINDOW_SECONDS: int = 30

#: Cap on the founder-typed ``reason`` field — keeps it toast-friendly.
_REASON_MAX_CHARS = 280


OntologyAction = Literal["retract", "correct"]


class RetractionSignal(BaseModel):
    """Founder-issued retraction or correction of an ontology node.

    Identity ``(id, workspace_id, actor_id)`` is the idempotency key —
    re-issuing the same correction id on the same workspace + actor is a
    no-op. ``node_ref`` is the stable id from the Inside graph (a vault path
    for garden notes / a concept id for canonical concepts). ``apply_at`` is
    the server-stamped deadline the worker / lazy resolver gates the actual
    tombstone write on; before it, an :meth:`RetractionService.undo` is honored.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    # --- Identity (required) ---
    id: uuid.UUID = Field(description="Idempotency key for this correction.")
    workspace_id: uuid.UUID = Field(description="Workspace the correction targets.")
    actor_id: uuid.UUID = Field(description="Founder issuing the correction.")

    # --- Target node (required) ---
    node_ref: str = Field(
        description=(
            "Stable id for the node — for garden notes this is the vault path "
            "(e.g. ``garden/seedling/foo.md``); for canonical concepts it is the "
            "concept id (e.g. ``rate-limit``)."
        ),
        min_length=1,
    )

    # --- Action (required) ---
    action: OntologyAction = Field(description="``retract`` or ``correct``.")

    # --- Lifecycle (required, server-stamped at intake) ---
    issued_at: datetime = Field(description="When the correction was issued (UTC).")
    apply_at: datetime = Field(
        description=(
            "When the undo window expires (= ``issued_at + UNDO_WINDOW_SECONDS``). "
            "After this, undo returns ``expired`` and the tombstone is committed."
        ),
    )

    # --- Optional founder-typed reason ---
    reason: str | None = Field(
        default=None,
        max_length=_REASON_MAX_CHARS,
        description="Optional founder-typed free text (max 280 chars).",
    )

    # --- Optional audit / source attribution ---
    source: str = Field(
        default="ontology_inspect_ui",
        description="Where the correction was issued from (audit attribution).",
        max_length=64,
    )


__all__ = ["UNDO_WINDOW_SECONDS", "OntologyAction", "RetractionSignal"]
