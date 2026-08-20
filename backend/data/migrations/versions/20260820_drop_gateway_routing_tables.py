"""BSGateway 라우팅 잔재 테이블 2개를 지운다 — ``model_catalog_entries`` / ``routing_logs``.

prod 실측 (2026-08-20): 두 테이블 모두 **0행**. 쓰는 프로덕션 코드가 0개였다 —
유일한 writer 였던 ``ModelCatalogRepository`` / ``RoutingLogsRepository`` 자체가
프로덕션 호출자 0인 죽은 모듈이었고, 이 커밋이 그것들도 함께 지운다.

이 서브시스템은 자기 미래를 스스로 적어뒀다: ``strategies.py`` 가
*"Wired into the dispatch path in Bundle 1.5c **when the LiteLLM hook lands**"*
라고 썼는데, 그 훅이 들어갈 ``backend/api/litellm_hook/`` 는 ``.py`` 가 하나도 없는
빈 디렉터리다. 훅은 오지 않았고, 라우팅은 형님 정책 ``bsvibe-no-implicit-routing``
에 따라 ``backend.dispatch`` 로 흐른다.

⚠️ ``run_routing_rules`` 는 **건드리지 않는다** — 형님이 실제로 쓰는 런 라우팅 규칙
테이블이고, 같은 패키지에 살 뿐 이 잔재와 무관하다.

행이 0 이므로 데이터 손실이 없다. ``downgrade`` 는 1.5b 마이그레이션이 만들던 모양을
그대로 복원한다.
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision: str = "drop_gateway_routing"
down_revision: Union[str, Sequence[str], None] = "drop_ingest_batches"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_index("ix_routing_logs_acct_timestamp", table_name="routing_logs")
    op.drop_index("ix_routing_logs_account_id", table_name="routing_logs")
    op.drop_index("ix_routing_logs_workspace_id", table_name="routing_logs")
    op.drop_index("ix_routing_logs_timestamp", table_name="routing_logs")
    op.drop_table("routing_logs")

    op.drop_index("ix_model_catalog_entries_account_id", table_name="model_catalog_entries")
    op.drop_index("ix_model_catalog_entries_workspace_id", table_name="model_catalog_entries")
    op.drop_table("model_catalog_entries")


def downgrade() -> None:
    # --- model_catalog_entries ---------------------------------------
    op.create_table(
        "model_catalog_entries",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column(
            "origin",
            sa.String(20),
            nullable=False,
        ),
        sa.Column("litellm_model", sa.String(255), nullable=True),
        sa.Column("litellm_params", postgresql.JSONB(), nullable=True),
        sa.Column("is_passthrough", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "origin IN ('custom', 'hide_system')",
            name="ck_model_catalog_origin",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workspace_id",
            "account_id",
            "name",
            name="uq_model_catalog_entries_acct_name",
        ),
    )
    op.create_index(
        "ix_model_catalog_entries_workspace_id",
        "model_catalog_entries",
        ["workspace_id"],
    )
    op.create_index(
        "ix_model_catalog_entries_account_id",
        "model_catalog_entries",
        ["account_id"],
    )

    # --- routing_logs -------------------------------------------------
    op.create_table(
        "routing_logs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("rule_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("user_text", sa.Text(), nullable=True),
        sa.Column("system_prompt", sa.Text(), nullable=True),
        sa.Column("token_count", sa.Integer(), nullable=True),
        sa.Column("conversation_turns", sa.Integer(), nullable=True),
        sa.Column("code_block_count", sa.Integer(), nullable=True),
        sa.Column("code_lines", sa.Integer(), nullable=True),
        sa.Column("has_error_trace", sa.Boolean(), nullable=True),
        sa.Column("tool_count", sa.Integer(), nullable=True),
        sa.Column("tier", sa.String(20), nullable=True),
        sa.Column("strategy", sa.String(40), nullable=True),
        sa.Column("score", sa.Integer(), nullable=True),
        sa.Column("original_model", sa.String(200), nullable=True),
        sa.Column("resolved_model", sa.String(200), nullable=True),
        sa.Column("embedding", Vector(None), nullable=True),
        sa.Column("bsvibe_task_type", sa.String(80), nullable=True),
        sa.Column("bsvibe_priority", sa.String(20), nullable=True),
        sa.Column("bsvibe_complexity_hint", sa.Integer(), nullable=True),
        sa.Column("decision_source", sa.String(40), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_routing_logs_timestamp", "routing_logs", ["timestamp"])
    op.create_index("ix_routing_logs_workspace_id", "routing_logs", ["workspace_id"])
    op.create_index("ix_routing_logs_account_id", "routing_logs", ["account_id"])
    op.create_index(
        "ix_routing_logs_acct_timestamp",
        "routing_logs",
        ["workspace_id", "account_id", "timestamp"],
    )
