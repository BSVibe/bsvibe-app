"""Domain dataclasses for canonicalization notes (Handoff §6, Class_Diagram §6).

Slice 1 ships only ``ConceptEntry`` + ``ActionEntry`` + nested validation/
execution records. Proposal/decision/policy/tombstone shapes are in scope for
later slices but are referenced via type aliases here so import sites can
stabilize without rewrites.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

# Per Handoff §6
ACTION_STATUSES: tuple[str, ...] = (
    "draft",
    "pending_approval",
    "applied",
    "rejected",
    "blocked",
    "expired",
    "failed",
    "superseded",
)

# Per Handoff §6, §7 (forbidden kinds excluded — see §6 Forbidden action kinds)
ACTION_KINDS: tuple[str, ...] = (
    "create-concept",
    "merge-concepts",
    "split-concept",
    "deprecate-concept",
    "restore-concept",
    "retag-notes",
    "update-policy",
    "create-decision",
)


@dataclass
class ConceptEntry:
    """Active concept registry entry (Handoff §3.1).

    Identity is the file stem under ``concepts/active/``. ``display`` comes
    from the H1 in the body, not frontmatter (per Handoff §0.2 — path and
    frontmatter have different jobs).
    """

    concept_id: str
    path: str
    display: str
    aliases: list[str]
    created_at: datetime
    updated_at: datetime
    source_action: str | None = None


@dataclass
class TombstoneEntry:
    """Merged-concept tombstone (Handoff §3.2)."""

    old_id: str
    path: str
    merged_into: str
    merged_at: datetime
    source_action: str | None = None


@dataclass
class DeprecatedEntry:
    """Deprecated concept registry entry (Handoff §3.3)."""

    concept_id: str
    path: str
    deprecated_at: datetime
    replacement: str | None = None
    reason: str | None = None
    source_action: str | None = None


@dataclass
class ResolveResult:
    """Tag resolution outcome (Handoff §11, Class_Diagram §6)."""

    status: str
    concept_id: str | None = None
    redirected_from: str | None = None
    ambiguous_candidates: list[str] = field(default_factory=list)
    pending_draft: str | None = None
    deprecated_replacement: str | None = None


# Per Handoff §11
RESOLVE_STATUSES: tuple[str, ...] = (
    "resolved",
    "new_candidate",
    "ambiguous",
    "blocked",
    "pending_candidate",
)

# Per Handoff §5
PROPOSAL_STATUSES: tuple[str, ...] = (
    "pending",
    "accepted",
    "rejected",
    "superseded",
    "expired",
)


@dataclass
class ProposalEntry:
    """Review-candidate proposal note (Handoff §5).

    Proposals have no execution power — they group evidence and link to
    one or more action drafts. Apply MUST happen on the linked action,
    not the proposal.
    """

    path: str
    kind: str
    status: str
    strategy: str
    generator: str
    generator_version: str
    proposal_score: float
    created_at: datetime
    updated_at: datetime
    expires_at: datetime
    freshness: dict[str, Any] = field(default_factory=dict)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    affected_paths: list[str] = field(default_factory=list)
    action_drafts: list[str] = field(default_factory=list)
    result_actions: list[str] = field(default_factory=list)


@dataclass
class ValidationResult:
    """Validation outcome embedded in action frontmatter (Handoff §6, §13)."""

    status: str = "not_run"
    hard_blocks: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ScoreResult:
    """Scoring outcome embedded in action frontmatter (Handoff §6, §13).

    Per §13 — required fields: stability_score, hard_blocks, risk_reasons,
    deterministic_evidence, model_evidence, human_evidence, scorer_version.
    Slice 1 leaves all fields at default. Slice 4 fills these from
    deterministic evidence; slice 5 adds model + human signals.
    """

    status: str = "not_run"
    stability_score: float | None = None
    scorer_version: str | None = None
    policy_profile_path: str | None = None
    hard_blocks: list[dict[str, Any]] = field(default_factory=list)
    risk_reasons: list[dict[str, Any]] = field(default_factory=list)
    deterministic_evidence: list[dict[str, Any]] = field(default_factory=list)
    model_evidence: list[dict[str, Any]] = field(default_factory=list)
    human_evidence: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class PermissionRecord:
    """Safe Mode permission decision (Handoff §6, §13)."""

    safe_mode: bool | None = None
    decision: str | None = None
    actor: str | None = None
    decided_at: datetime | None = None


@dataclass
class ExecutionRecord:
    """Apply outcome embedded in action frontmatter (Handoff §6, §13)."""

    status: str = "not_run"
    applied_at: datetime | None = None
    error: str | None = None


@dataclass
class ActionEntry:
    """Typed action draft / audit record (Handoff §6).

    Path and kind are derived from the file location, not from frontmatter
    fields (per Handoff §0.2). The dataclass keeps both for in-memory
    convenience but ``kind`` is never written to disk.
    """

    path: str
    kind: str
    status: str
    action_schema_version: str
    params: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    expires_at: datetime
    source_proposal: str | None = None
    freshness: dict[str, Any] = field(default_factory=dict)
    validation: ValidationResult = field(default_factory=ValidationResult)
    scoring: ScoreResult = field(default_factory=ScoreResult)
    permission: PermissionRecord = field(default_factory=PermissionRecord)
    execution: ExecutionRecord = field(default_factory=ExecutionRecord)
    affected_paths: list[str] = field(default_factory=list)
    supersedes: list[str] = field(default_factory=list)
    superseded_by: str | None = None
    # Cross-cutting provenance evidence (Handoff §11 ingest_pending_candidate
    # is appended here when a duplicate ingest sighting links to an existing
    # non-terminal draft).
    evidence: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ApplyResult:
    """Outcome returned to the API/CLI caller (Class_Diagram §6)."""

    action_path: str
    final_status: str
    affected_paths: list[str]
    domain_effects: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None


@dataclass
class ExpireResult:
    """Outcome of ``service.expire_stale()`` (Handoff §15.3 canon-expire)."""

    expired_actions: list[str] = field(default_factory=list)
    expired_proposals: list[str] = field(default_factory=list)


# Per Handoff §8.1
DECISION_STATUSES: tuple[str, ...] = (
    "active",
    "superseded",
    "retracted",
    "expired",
)

# Per Handoff §8.1
DECAY_PROFILES: tuple[str, ...] = (
    "definitional",
    "semantic",
    "episodic",
    "procedural",
    "affective",
)


@dataclass
class DecisionEntry:
    """Decision-memory note (Handoff §8.1).

    ``cannot-link`` defaults to ``decay.profile: definitional`` (no decay).
    ``effective_strength`` is computed by ``DecisionMemory`` using the
    existing ``bsage.garden.confidence`` decay model:
    ``base_confidence * 0.5 ** (days_since_confirmed / halflife_days)``.
    """

    path: str
    kind: str
    status: str
    maturity: str
    decision_schema_version: str
    subjects: tuple[str, ...]
    base_confidence: float
    last_confirmed_at: datetime
    decay_profile: str
    decay_halflife_days: int | None
    valid_from: datetime
    created_at: datetime
    updated_at: datetime
    review_after: datetime | None = None
    expires_at: datetime | None = None
    policy_profile_path: str | None = None
    source_proposal: str | None = None
    source_action: str | None = None
    supersedes: list[str] = field(default_factory=list)
    superseded_by: str | None = None


# Per Handoff §8.2
POLICY_STATUSES: tuple[str, ...] = (
    "active",
    "superseded",
    "expired",
    "draft",
)


@dataclass
class PolicyEntry:
    """Policy-profile note (Handoff §8.2)."""

    path: str
    kind: str
    status: str
    profile_name: str
    priority: int
    scope: dict[str, Any]
    policy_schema_version: str
    valid_from: datetime
    params: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    expires_at: datetime | None = None
    learned_from: dict[str, Any] = field(default_factory=dict)
    supersedes: list[str] = field(default_factory=list)
    superseded_by: str | None = None
