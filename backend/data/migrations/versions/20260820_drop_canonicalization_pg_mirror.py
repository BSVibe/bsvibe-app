"""캐노니컬라이제이션 Postgres 미러를 지운다 — 코드베이스가 스스로 자백한 producer-less 표현.

prod 실측 (2026-08-20) — 다섯 테이블 전부 **0행**: ``canonical_anchors`` ·
``canonicalization_proposals`` · ``canonicalization_decisions`` ·
``canonicalization_policies`` · ``retrieval_queries``.

이건 감사자의 판단이 아니라 코드의 자백이다.

``backend/api/v1/decisions/__init__.py``
    *"proposals are markdown notes in the vault, NOT rows in the (currently
    producer-less) ``canonicalization_proposals`` DB table."*

``backend/api/v1/workspace_compliance.py``
    *"It is emphatically NOT the ``canonical_anchors`` DB table, which is
    producer-less: nothing writes it, so reading it under-reported the founder's
    knowledge as empty for every workspace (a GDPR Art. 15/20 defect)."*

→ 이 미러는 이미 실제 규제 결함을 일으켰다. 진짜 SoT 는 볼트 파일이다.

⚠️ ``note_embeddings`` 는 건드리지 않는다 — 같은 ``retrieval`` 모듈에 살지만
pgvector 임베딩을 담고 ``PgNoteVectorBackend`` 가 쓰며 prod 에 1,700행 넘게 있다.

이 마이그레이션의 ``upgrade`` 는 ``bundle_k_knowledge`` 의 ``downgrade`` 를, 이
``downgrade`` 는 그 ``upgrade`` 를 그대로 미러한다 — 손으로 다시 적으면 드리프트가
난다. ``ingest_batches`` 블록만 뺐다 (#786 이 이미 지웠다).
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "drop_canon_pg_mirror"
down_revision: Union[str, Sequence[str], None] = "drop_budget_policies"
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
    op.drop_index("ix_retrieval_queries_ws_created", table_name="retrieval_queries")
    op.drop_index("ix_retrieval_queries_workspace_id", table_name="retrieval_queries")
    op.drop_table("retrieval_queries")

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


def downgrade() -> None:
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
