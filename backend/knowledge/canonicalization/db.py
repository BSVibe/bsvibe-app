"""Canonicalization persistence schema — proposals, decisions, policies, anchors.

Hybrid persistence model (Phase 1):

* Nodes / relationships / provenance remain in the Markdown vault (FS-as-SoT).
* Canonicalization state (queue + decision log + policy registry +
  canonical-anchor index) lives in Postgres so multi-tenant workspaces can
  query and audit it under row-level security (RLS, defense-in-depth).

Per-workspace scoping is enforced via ``workspace_id`` NOT NULL on every
row plus composite indexes that pair it with the natural query keys.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import JSON, DateTime, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class CanonicalizationBase(DeclarativeBase):
    """Declarative base for canonicalization-domain tables."""


# Enum domains lifted verbatim from
# :mod:`backend.knowledge.canonicalization.paths`. SQLAlchemy needs concrete
# StrEnum types so Postgres ENUMs are named and migratable.


class ActionKind(StrEnum):
    """Handoff §6, §7 — the 8 typed mutations the canon system can apply."""

    CREATE_CONCEPT = "create-concept"
    MERGE_CONCEPTS = "merge-concepts"
    SPLIT_CONCEPT = "split-concept"
    DEPRECATE_CONCEPT = "deprecate-concept"
    RESTORE_CONCEPT = "restore-concept"
    RETAG_NOTES = "retag-notes"
    UPDATE_POLICY = "update-policy"
    CREATE_DECISION = "create-decision"


class ProposalKind(StrEnum):
    """Handoff §5 — the 6 proposal shapes the proposer emits."""

    MERGE_CONCEPTS = "merge-concepts"
    CREATE_CONCEPT = "create-concept"
    RETAG_NOTES = "retag-notes"
    POLICY_UPDATE = "policy-update"
    POLICY_CONFLICT = "policy-conflict"
    DECISION_REVIEW = "decision-review"


class ProposalStatus(StrEnum):
    """Queue lifecycle. Default is ``PENDING`` (queue-only Safe Mode)."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    SUPERSEDED = "superseded"


class DecisionKind(StrEnum):
    """Handoff §8.1 — directional decisions captured from founder approvals."""

    CANNOT_LINK = "cannot-link"
    MUST_LINK = "must-link"


class PolicyKind(StrEnum):
    """Handoff §8.2 — the 3 policy classes governing automation."""

    STALENESS = "staleness"
    MERGE_AUTO_APPLY = "merge-auto-apply"
    DECISION_MATURITY = "decision-maturity"


class CanonicalAnchor(CanonicalizationBase):
    """Per-workspace concept index. Each row = one canonical name.

    The Markdown vault still owns the concept files; this table provides a
    queryable index for retrieval ranking and proposal scoring.
    """

    __tablename__ = "canonical_anchors"
    __table_args__ = (
        UniqueConstraint("workspace_id", "name", name="uq_canonical_anchors_ws_name"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(),
        onupdate=lambda: datetime.now(),
    )


class CanonicalizationProposal(CanonicalizationBase):
    """Pending proposal — the queue surface for Safe Mode approvals."""

    __tablename__ = "canonicalization_proposals"
    __table_args__ = (
        Index("ix_canon_proposals_ws_status", "workspace_id", "status"),
        Index("ix_canon_proposals_ws_kind", "workspace_id", "proposal_kind"),
        Index("ix_canon_proposals_action_path", "workspace_id", "action_path"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    proposal_kind: Mapped[ProposalKind] = mapped_column(
        SAEnum(
            ProposalKind,
            name="canonicalization_proposal_kind_enum",
            values_callable=lambda ec: [m.value for m in ec],
        ),
        nullable=False,
    )
    action_kind: Mapped[ActionKind] = mapped_column(
        SAEnum(
            ActionKind,
            name="canonicalization_action_kind_enum",
            values_callable=lambda ec: [m.value for m in ec],
        ),
        nullable=False,
    )
    action_path: Mapped[str] = mapped_column(String(512), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    evidence: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    status: Mapped[ProposalStatus] = mapped_column(
        SAEnum(
            ProposalStatus,
            name="canonicalization_proposal_status_enum",
            values_callable=lambda ec: [m.value for m in ec],
        ),
        nullable=False,
        default=ProposalStatus.PENDING,
    )
    score: Mapped[int | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now()
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CanonicalizationDecision(CanonicalizationBase):
    """Resolved decision log. One row per founder approve/deny/supersede."""

    __tablename__ = "canonicalization_decisions"
    __table_args__ = (
        Index("ix_canon_decisions_ws_kind", "workspace_id", "decision_kind"),
        Index("ix_canon_decisions_proposal", "proposal_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    proposal_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("canonicalization_proposals.id", ondelete="SET NULL"), nullable=True
    )
    decision_kind: Mapped[DecisionKind] = mapped_column(
        SAEnum(
            DecisionKind,
            name="canonicalization_decision_kind_enum",
            values_callable=lambda ec: [m.value for m in ec],
        ),
        nullable=False,
    )
    actor_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    supersedes_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("canonicalization_decisions.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now()
    )


class CanonicalizationPolicy(CanonicalizationBase):
    """Per-workspace policy registry. One row per (workspace, policy_kind)."""

    __tablename__ = "canonicalization_policies"
    __table_args__ = (
        UniqueConstraint("workspace_id", "policy_kind", name="uq_canon_policies_ws_kind"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    policy_kind: Mapped[PolicyKind] = mapped_column(
        SAEnum(
            PolicyKind,
            name="canonicalization_policy_kind_enum",
            values_callable=lambda ec: [m.value for m in ec],
        ),
        nullable=False,
    )
    config: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(),
        onupdate=lambda: datetime.now(),
    )
