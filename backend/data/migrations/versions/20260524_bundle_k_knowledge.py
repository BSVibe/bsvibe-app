"""Bundle K — canonicalization state (proposals, decisions, policies, anchors)
+ ingest_batches + retrieval_queries analytics. Hybrid persistence model:
graph nodes / relationships / provenance remain in the Markdown vault
(FS-as-SoT); only canon queue / decision log / policy registry / analytics
land in Postgres.

Revision ID: bundle_k_knowledge
Revises: bundle1_5b_routing_embed
Create Date: 2026-05-24
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "bundle_k_knowledge"
down_revision: Union[str, Sequence[str], None] = "bundle1_5b_routing_embed"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Postgres ENUM domains. ``create_type=False`` on each column ref — the
# enum is created exactly once by the first DDL that issues it (CREATE TYPE
# IF NOT EXISTS pattern via ``checkfirst``).

_ACTION_KIND = postgresql.ENUM(
    "create-concept",
    "merge-concepts",
    "split-concept",
    "deprecate-concept",
    "restore-concept",
    "retag-notes",
    "update-policy",
    "create-decision",
    name="canonicalization_action_kind_enum",
    create_type=False,
)

_PROPOSAL_KIND = postgresql.ENUM(
    "merge-concepts",
    "create-concept",
    "retag-notes",
    "policy-update",
    "policy-conflict",
    "decision-review",
    name="canonicalization_proposal_kind_enum",
    create_type=False,
)

_PROPOSAL_STATUS = postgresql.ENUM(
    "pending",
    "approved",
    "rejected",
    "expired",
    "superseded",
    name="canonicalization_proposal_status_enum",
    create_type=False,
)

_DECISION_KIND = postgresql.ENUM(
    "cannot-link",
    "must-link",
    name="canonicalization_decision_kind_enum",
    create_type=False,
)

_POLICY_KIND = postgresql.ENUM(
    "staleness",
    "merge-auto-apply",
    "decision-maturity",
    name="canonicalization_policy_kind_enum",
    create_type=False,
)


def upgrade() -> None:
    bind = op.get_bind()

    # Create ENUM types once via checkfirst. The ``ENUM(create_type=False)``
    # bindings above prevent SQLAlchemy from re-issuing CREATE TYPE later.
    sa.Enum(
        "create-concept",
        "merge-concepts",
        "split-concept",
        "deprecate-concept",
        "restore-concept",
        "retag-notes",
        "update-policy",
        "create-decision",
        name="canonicalization_action_kind_enum",
    ).create(bind, checkfirst=True)
    sa.Enum(
        "merge-concepts",
        "create-concept",
        "retag-notes",
        "policy-update",
        "policy-conflict",
        "decision-review",
        name="canonicalization_proposal_kind_enum",
    ).create(bind, checkfirst=True)
    sa.Enum(
        "pending",
        "approved",
        "rejected",
        "expired",
        "superseded",
        name="canonicalization_proposal_status_enum",
    ).create(bind, checkfirst=True)
    sa.Enum(
        "cannot-link",
        "must-link",
        name="canonicalization_decision_kind_enum",
    ).create(bind, checkfirst=True)
    sa.Enum(
        "staleness",
        "merge-auto-apply",
        "decision-maturity",
        name="canonicalization_policy_kind_enum",
    ).create(bind, checkfirst=True)

    op.create_table(
        "canonical_anchors",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("workspace_id", "name", name="uq_canonical_anchors_ws_name"),
    )
    op.create_index("ix_canonical_anchors_workspace_id", "canonical_anchors", ["workspace_id"])

    op.create_table(
        "canonicalization_proposals",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("proposal_kind", _PROPOSAL_KIND, nullable=False),
        sa.Column("action_kind", _ACTION_KIND, nullable=False),
        sa.Column("action_path", sa.String(length=512), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=True),
        sa.Column("status", _PROPOSAL_STATUS, nullable=False),
        sa.Column("score", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_canonicalization_proposals_workspace_id",
        "canonicalization_proposals",
        ["workspace_id"],
    )
    op.create_index(
        "ix_canon_proposals_ws_status",
        "canonicalization_proposals",
        ["workspace_id", "status"],
    )
    op.create_index(
        "ix_canon_proposals_ws_kind",
        "canonicalization_proposals",
        ["workspace_id", "proposal_kind"],
    )
    op.create_index(
        "ix_canon_proposals_action_path",
        "canonicalization_proposals",
        ["workspace_id", "action_path"],
    )

    op.create_table(
        "canonicalization_decisions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "proposal_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("canonicalization_proposals.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("decision_kind", _DECISION_KIND, nullable=False),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column(
            "supersedes_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("canonicalization_decisions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_canonicalization_decisions_workspace_id",
        "canonicalization_decisions",
        ["workspace_id"],
    )
    op.create_index(
        "ix_canon_decisions_ws_kind",
        "canonicalization_decisions",
        ["workspace_id", "decision_kind"],
    )
    op.create_index("ix_canon_decisions_proposal", "canonicalization_decisions", ["proposal_id"])

    op.create_table(
        "canonicalization_policies",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("policy_kind", _POLICY_KIND, nullable=False),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("workspace_id", "policy_kind", name="uq_canon_policies_ws_kind"),
    )
    op.create_index(
        "ix_canonicalization_policies_workspace_id",
        "canonicalization_policies",
        ["workspace_id"],
    )

    op.create_table(
        "ingest_batches",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("seed_count", sa.Integer(), nullable=False),
        sa.Column("decisions", sa.JSON(), nullable=False),
        sa.Column("elapsed_ms", sa.Integer(), nullable=False),
        sa.Column("model_used", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_ingest_batches_workspace_id", "ingest_batches", ["workspace_id"])
    op.create_index(
        "ix_ingest_batches_ws_created", "ingest_batches", ["workspace_id", "created_at"]
    )

    op.create_table(
        "retrieval_queries",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("query_text", sa.Text(), nullable=False),
        sa.Column("top_k", sa.Integer(), nullable=False),
        sa.Column("result_count", sa.Integer(), nullable=False),
        sa.Column("elapsed_ms", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_retrieval_queries_workspace_id", "retrieval_queries", ["workspace_id"])
    op.create_index(
        "ix_retrieval_queries_ws_created",
        "retrieval_queries",
        ["workspace_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_retrieval_queries_ws_created", table_name="retrieval_queries")
    op.drop_index("ix_retrieval_queries_workspace_id", table_name="retrieval_queries")
    op.drop_table("retrieval_queries")

    op.drop_index("ix_ingest_batches_ws_created", table_name="ingest_batches")
    op.drop_index("ix_ingest_batches_workspace_id", table_name="ingest_batches")
    op.drop_table("ingest_batches")

    op.drop_index(
        "ix_canonicalization_policies_workspace_id", table_name="canonicalization_policies"
    )
    op.drop_table("canonicalization_policies")

    op.drop_index("ix_canon_decisions_proposal", table_name="canonicalization_decisions")
    op.drop_index("ix_canon_decisions_ws_kind", table_name="canonicalization_decisions")
    op.drop_index(
        "ix_canonicalization_decisions_workspace_id", table_name="canonicalization_decisions"
    )
    op.drop_table("canonicalization_decisions")

    op.drop_index("ix_canon_proposals_action_path", table_name="canonicalization_proposals")
    op.drop_index("ix_canon_proposals_ws_kind", table_name="canonicalization_proposals")
    op.drop_index("ix_canon_proposals_ws_status", table_name="canonicalization_proposals")
    op.drop_index(
        "ix_canonicalization_proposals_workspace_id", table_name="canonicalization_proposals"
    )
    op.drop_table("canonicalization_proposals")

    op.drop_index("ix_canonical_anchors_workspace_id", table_name="canonical_anchors")
    op.drop_table("canonical_anchors")

    bind = op.get_bind()
    sa.Enum(name="canonicalization_policy_kind_enum").drop(bind, checkfirst=True)
    sa.Enum(name="canonicalization_decision_kind_enum").drop(bind, checkfirst=True)
    sa.Enum(name="canonicalization_proposal_status_enum").drop(bind, checkfirst=True)
    sa.Enum(name="canonicalization_proposal_kind_enum").drop(bind, checkfirst=True)
    sa.Enum(name="canonicalization_action_kind_enum").drop(bind, checkfirst=True)
