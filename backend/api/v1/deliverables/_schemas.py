"""Shared Pydantic response models + payload-mapping helpers for the
``/api/v1/deliverables/*`` endpoint group (Lift §17.9 sub-file).

Two responsibilities — both *thin* per D35:

* Response shapes used by more than one endpoint module
  (:class:`DeliverableResponse`, :class:`VerificationReport`,
  :class:`DeliverableReportResponse`, :class:`ArtifactContentResponse`).
* Defensive payload mappers — the ``Deliverable.payload`` column is
  free-form JSON written by the orchestrator; missing/odd values degrade to
  ``None`` / ``[]`` so a malformed row never 500s the response model.

The endpoint sub-files (``list_get``, ``proof``, ``retract``) import from
here; no business logic lives in this module.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from backend.workflow.application.verification_service import (
    LEGACY_RETRIEVED_KNOWLEDGE_RATIONALE,
    RETRIEVED_KNOWLEDGE_RATIONALE,
)
from backend.workflow.infrastructure.db import (
    Deliverable,
    DeliverableType,
    VerificationOutcome,
    VerificationResult,
)

# Read cap for artifact content. A produced source file is small; this guards
# against an accidental multi-MB log/blob slipping into a JSON body. Beyond it
# the response carries the first ``MAX_CONTENT_BYTES`` decoded as text with
# ``truncated: true`` so the viewer can show a calm "showing the first part"
# note rather than streaming an unbounded payload.
MAX_CONTENT_BYTES = 256 * 1024


class DeliverableResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    run_id: uuid.UUID
    workspace_id: uuid.UUID
    deliverable_type: DeliverableType
    summary: str | None = None
    artifact_refs: list[str] = []
    artifact_uri: str | None = None
    diff_url: str | None = None
    # B4 trust-integrity: True ONLY when a PASSED VerificationResult exists for
    # the producing run. The founder-facing "verified" badge MUST derive from
    # this backend-authoritative flag, never from a Deliverable merely existing.
    # Defaults False so a hollow row (no PASSED proof) reads honestly as
    # unverified / needs-review rather than a green "verified".
    verified: bool = False
    created_at: datetime


class VerificationReport(BaseModel):
    """One VerificationResult — the "how BSVibe checked this" proof.

    ``contract`` is the work LLM's declared list of checks (the checks BSVibe
    promised to run) and ``result`` is the execution outcome of running them;
    both are free-form JSON (shape varies by verifier), so they are surfaced
    verbatim and rendered defensively by the report view.
    """

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    outcome: VerificationOutcome
    contract: dict[str, Any] = {}
    result: dict[str, Any] = {}
    created_at: datetime


class DeliverableReportResponse(BaseModel):
    """The glass-box proof for one shipped deliverable: the founder's original
    request, the artifact, and the verification(s) recorded for its producing
    run. ``request`` is the founder's Direction that led to this work (pulled
    from the producing run's free-form payload), so the report reads as a
    document — request → what was built → how it was checked. ``None`` when the
    run carries no recorded intent."""

    model_config = ConfigDict(extra="forbid")

    deliverable: DeliverableResponse
    request: str | None = None
    # B4 trust-integrity: True ONLY when ≥1 PASSED VerificationResult is recorded
    # for the producing run (mirrors ``deliverable.verified``); else needs-review.
    verified: bool = False
    verifications: list[VerificationReport] = []
    # R8 — footer mirrors the Brief: a HELD delivery (held_delivery_item_id set)
    # shows Approve & ship / Decline; only run_status=="shipped" shows Rollback.
    run_status: str | None = None
    held_delivery_item_id: uuid.UUID | None = None
    # G2 — knowledge the agent REFERENCED: canon/prior decisions/rejections (deduped).
    references: list[str] = []
    # R2b — knowledge the run NEWLY wrote this time (founder decisions it resolved
    # + approaches it rejected). The report's "Learned" group; empty when none.
    learned: list[str] = []
    # R1 — chat-composed plain-language "what this did" (cached); falls back to request.
    narrative: str | None = None


class DeliverableDiffResponse(BaseModel):
    """The run's captured old↔new changes as a unified (``git diff``) patch.

    Captured at verify-time for product runs (while the run worktree is alive)
    and stored on ``Deliverable.payload``. ``diff`` is ``None`` for deliverables
    with no captured diff — a non-product (Direct) run, or a row produced before
    this feature — in which case the viewer falls back to rendering the produced
    file content as additions. ``truncated`` flags that the diff was larger than
    the stored cap (only the leading part is returned)."""

    model_config = ConfigDict(extra="forbid")

    diff: str | None = None
    truncated: bool = False


class ArtifactContentResponse(BaseModel):
    """The produced CONTENT of one artifact file, read-only.

    Served from the persisted run workspace
    (``<run_workspace_root>/<run_id>/<ref>``) so the founder can SEE what the
    agent actually wrote — not just a filename or a (often-null) git link.

    ``content`` is the file decoded as UTF-8 text with ``errors="replace"``
    (lossy but never throws), capped at 256 KiB. ``truncated`` flags that the
    file was larger than the cap (only the leading bytes are returned).
    ``binary`` flags a non-text file, in which case ``content`` is a short
    "binary file, N bytes" note rather than the raw bytes.
    """

    model_config = ConfigDict(extra="forbid")

    ref: str
    content: str
    truncated: bool = False
    binary: bool = False


# ---------------------------------------------------------------------------
# Defensive payload mappers — Deliverable.payload is free-form JSON.
# ---------------------------------------------------------------------------


def summary_of(payload: dict[str, Any]) -> str | None:
    """Pull a string ``summary`` out of the free-form payload, else ``None``."""
    value = payload.get("summary")
    return value if isinstance(value, str) else None


def artifact_refs_of(payload: dict[str, Any]) -> list[str]:
    """Pull a list of string ``artifact_refs`` out of the payload, else ``[]``."""
    value = payload.get("artifact_refs")
    if isinstance(value, list):
        return [str(item) for item in value]
    return []


def diff_of(payload: dict[str, Any]) -> tuple[str | None, bool]:
    """Pull the captured unified diff + truncation flag out of the payload.

    Returns ``(diff, truncated)`` — ``(None, False)`` when no diff was captured
    (a non-product run / a pre-feature row), defensively coercing odd shapes."""
    diff = payload.get("diff")
    if not isinstance(diff, str):
        return None, False
    return diff, payload.get("diff_truncated") is True


def request_text_of(payload: dict[str, Any]) -> str | None:
    """Pull the founder's Direction out of the producing run's free-form payload
    (``intent_text`` from intake, or ``text``), the same keys the run-detail
    trigger context reads. ``None`` when neither is a non-empty string."""
    for key in ("intent_text", "text"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def to_response(row: Deliverable, *, verified: bool = False) -> DeliverableResponse:
    payload = row.payload if isinstance(row.payload, dict) else {}
    return DeliverableResponse(
        id=row.id,
        run_id=row.run_id,
        workspace_id=row.workspace_id,
        deliverable_type=row.deliverable_type,
        summary=summary_of(payload),
        artifact_refs=artifact_refs_of(payload),
        artifact_uri=row.artifact_uri,
        diff_url=row.diff_url,
        verified=verified,
        created_at=row.created_at,
    )


def to_verification(row: VerificationResult) -> VerificationReport:
    contract = row.contract if isinstance(row.contract, dict) else {}
    result = row.result if isinstance(row.result, dict) else {}
    return VerificationReport(
        id=row.id,
        outcome=row.outcome,
        contract=contract,
        result=result,
        created_at=row.created_at,
    )


def references_of(verifications: list[VerificationReport]) -> list[str]:
    """The referenced-knowledge statements across a run's verifications (G2).

    Pulls the criteria of every judge check stamped with
    :data:`~backend.workflow.application.verification_service.RETRIEVED_KNOWLEDGE_RATIONALE`
    (the retriever's canon / prior-decision / prior-rejection fold), deduped in
    first-seen order. A run may record several verifications (re-attempts), so
    the same statement can recur — it surfaces once. Defensive against malformed
    contract JSON: any non-conforming shape contributes nothing, never raises."""
    references: list[str] = []
    seen: set[str] = set()
    for verification in verifications:
        checks = verification.contract.get("checks")
        if not isinstance(checks, list):
            continue
        for check in checks:
            if not isinstance(check, dict):
                continue
            # Current marker OR the legacy ("BSage") one on historical rows.
            if check.get("rationale") not in (
                RETRIEVED_KNOWLEDGE_RATIONALE,
                LEGACY_RETRIEVED_KNOWLEDGE_RATIONALE,
            ):
                continue
            criteria = check.get("criteria")
            if not isinstance(criteria, list):
                continue
            for item in criteria:
                statement = str(item).strip()
                if statement and statement not in seen:
                    seen.add(statement)
                    references.append(statement)
    return references
