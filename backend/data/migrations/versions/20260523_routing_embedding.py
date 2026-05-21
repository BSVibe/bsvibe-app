"""Bundle 1.5b — pgvector ext + intent_definitions + intent_examples +
account_embedding_settings + model_catalog_entries + routing_logs.

Revision ID: bundle1_5b_routing_embed
Revises: bundle1_5a_rules
Create Date: 2026-05-23
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision: str = "bundle1_5b_routing_embed"
down_revision: Union[str, Sequence[str], None] = "bundle1_5a_rules"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # pgvector — used by intent_examples.embedding + routing_logs.embedding.
    # ``IF NOT EXISTS`` so devcontainer reuses + repeat-runs are safe.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # --- account_embedding_settings ----------------------------------
    op.create_table(
        "account_embedding_settings",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("config", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "account_id", name="uq_account_embedding_settings"),
    )
    op.create_index(
        "ix_account_embedding_settings_workspace_id",
        "account_embedding_settings",
        ["workspace_id"],
    )
    op.create_index(
        "ix_account_embedding_settings_account_id",
        "account_embedding_settings",
        ["account_id"],
    )

    # --- intent_definitions ------------------------------------------
    op.create_table(
        "intent_definitions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("threshold", sa.Float(), nullable=False, server_default="0.65"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workspace_id",
            "account_id",
            "name",
            name="uq_intent_definitions_acct_name",
        ),
    )
    op.create_index(
        "ix_intent_definitions_workspace_id",
        "intent_definitions",
        ["workspace_id"],
    )
    op.create_index(
        "ix_intent_definitions_account_id",
        "intent_definitions",
        ["account_id"],
    )

    # --- intent_examples ---------------------------------------------
    op.create_table(
        "intent_examples",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("intent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        # Variable dim — accounts pick their own model. Add a HNSW index
        # in a follow-up once a workspace fixes its embedding model.
        sa.Column("embedding", Vector(None), nullable=True),
        sa.Column("embedding_model", sa.String(120), nullable=True),
        sa.Column("dimension", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["intent_id"], ["intent_definitions.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_intent_examples_intent_id", "intent_examples", ["intent_id"])
    op.create_index("ix_intent_examples_workspace_id", "intent_examples", ["workspace_id"])
    op.create_index("ix_intent_examples_account_id", "intent_examples", ["account_id"])

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
        sa.Column("nexus_task_type", sa.String(80), nullable=True),
        sa.Column("nexus_priority", sa.String(20), nullable=True),
        sa.Column("nexus_complexity_hint", sa.Integer(), nullable=True),
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


def downgrade() -> None:
    op.drop_index("ix_routing_logs_acct_timestamp", table_name="routing_logs")
    op.drop_index("ix_routing_logs_account_id", table_name="routing_logs")
    op.drop_index("ix_routing_logs_workspace_id", table_name="routing_logs")
    op.drop_index("ix_routing_logs_timestamp", table_name="routing_logs")
    op.drop_table("routing_logs")

    op.drop_index("ix_model_catalog_entries_account_id", table_name="model_catalog_entries")
    op.drop_index("ix_model_catalog_entries_workspace_id", table_name="model_catalog_entries")
    op.drop_table("model_catalog_entries")

    op.drop_index("ix_intent_examples_account_id", table_name="intent_examples")
    op.drop_index("ix_intent_examples_workspace_id", table_name="intent_examples")
    op.drop_index("ix_intent_examples_intent_id", table_name="intent_examples")
    op.drop_table("intent_examples")

    op.drop_index("ix_intent_definitions_account_id", table_name="intent_definitions")
    op.drop_index("ix_intent_definitions_workspace_id", table_name="intent_definitions")
    op.drop_table("intent_definitions")

    op.drop_index(
        "ix_account_embedding_settings_account_id",
        table_name="account_embedding_settings",
    )
    op.drop_index(
        "ix_account_embedding_settings_workspace_id",
        table_name="account_embedding_settings",
    )
    op.drop_table("account_embedding_settings")

    # Leave the pgvector extension installed — other tables (BSage's
    # incoming migration) may depend on it. Dropping the extension is
    # an operator concern, not a downgrade concern.
