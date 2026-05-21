"""Bundle 1 initial — ModelAccount, AccountBudgetPolicy, AuditEvent, AuditOutbox.

Revision ID: bundle1_initial
Revises: None (first revision)
Create Date: 2026-05-21
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "bundle1_initial"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ModelAccount — workspace-scoped LLM provider credentials
    op.create_table(
        "model_accounts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("label", sa.String(128), nullable=False),
        sa.Column("litellm_model", sa.String(255), nullable=False),
        sa.Column("api_base", sa.String(512), nullable=True),
        sa.Column("api_key_encrypted", sa.String(1024), nullable=False),
        sa.Column("data_jurisdiction", sa.String(16), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("extra_params", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "account_id", "label", name="uq_model_account_label"),
    )
    op.create_index(
        "ix_model_accounts_workspace_account",
        "model_accounts",
        ["workspace_id", "account_id"],
    )
    op.create_index("ix_model_accounts_workspace_id", "model_accounts", ["workspace_id"])
    op.create_index("ix_model_accounts_account_id", "model_accounts", ["account_id"])
    op.create_index("ix_model_accounts_data_jurisdiction", "model_accounts", ["data_jurisdiction"])

    # AccountBudgetPolicy — gateway-domain enforcement scoped to account_id
    budget_scope = postgresql.ENUM("daily", "monthly", name="budget_scope_enum", create_type=False)
    budget_enforcement = postgresql.ENUM(
        "block", "warn", "log", name="budget_enforcement_enum", create_type=False
    )
    budget_scope.create(op.get_bind(), checkfirst=True)
    budget_enforcement.create(op.get_bind(), checkfirst=True)
    op.create_table(
        "account_budget_policies",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("scope", budget_scope, nullable=False),
        sa.Column("cost_cap_cents", sa.Integer(), nullable=False),
        sa.Column("enforcement", budget_enforcement, nullable=False, server_default="block"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "account_id", "scope", name="uq_account_budget_scope"),
    )
    op.create_index(
        "ix_account_budget_policies_workspace_id",
        "account_budget_policies",
        ["workspace_id"],
    )
    op.create_index(
        "ix_account_budget_policies_account_id",
        "account_budget_policies",
        ["account_id"],
    )

    # AuditEvent — supervisor denormalised audit row
    op.create_table(
        "audit_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("agent_id", sa.String(255), nullable=False),
        sa.Column("workspace_id", sa.String(255), nullable=True),
        sa.Column("tenant_id", sa.String(255), nullable=True),
        sa.Column("source", sa.String(255), nullable=False),
        sa.Column("event_type", sa.String(255), nullable=False),
        sa.Column("action", sa.String(255), nullable=False),
        sa.Column("target", sa.String(1024), nullable=False),
        sa.Column("metadata_json", postgresql.JSONB(), nullable=True),
        sa.Column("allowed", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("explanation_json", postgresql.JSONB(), nullable=True),
        sa.Column("feedback_json", postgresql.JSONB(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audit_events_agent_id", "audit_events", ["agent_id"])
    op.create_index("ix_audit_events_workspace_id", "audit_events", ["workspace_id"])
    op.create_index("ix_audit_events_tenant_id", "audit_events", ["tenant_id"])
    op.create_index("ix_audit_events_event_type", "audit_events", ["event_type"])
    op.create_index(
        "ix_audit_events_workspace_created_at",
        "audit_events",
        ["workspace_id", "created_at"],
    )
    op.create_index(
        "ix_audit_events_event_type_created_at",
        "audit_events",
        ["event_type", "created_at"],
    )

    # AuditOutbox — in-tx relay queue
    op.create_table(
        "audit_outbox",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("event_id", sa.String(64), nullable=False),
        sa.Column("event_type", sa.String(128), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dead_letter", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_id", name="uq_audit_outbox_event_id"),
    )
    op.create_index(
        "ix_audit_outbox_undelivered",
        "audit_outbox",
        ["delivered_at", "next_attempt_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_audit_outbox_undelivered", table_name="audit_outbox")
    op.drop_table("audit_outbox")

    op.drop_index("ix_audit_events_event_type_created_at", table_name="audit_events")
    op.drop_index("ix_audit_events_workspace_created_at", table_name="audit_events")
    op.drop_index("ix_audit_events_event_type", table_name="audit_events")
    op.drop_index("ix_audit_events_tenant_id", table_name="audit_events")
    op.drop_index("ix_audit_events_workspace_id", table_name="audit_events")
    op.drop_index("ix_audit_events_agent_id", table_name="audit_events")
    op.drop_table("audit_events")

    op.drop_index("ix_account_budget_policies_account_id", table_name="account_budget_policies")
    op.drop_index("ix_account_budget_policies_workspace_id", table_name="account_budget_policies")
    op.drop_table("account_budget_policies")
    sa.Enum(name="budget_enforcement_enum").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="budget_scope_enum").drop(op.get_bind(), checkfirst=True)

    op.drop_index("ix_model_accounts_data_jurisdiction", table_name="model_accounts")
    op.drop_index("ix_model_accounts_account_id", table_name="model_accounts")
    op.drop_index("ix_model_accounts_workspace_id", table_name="model_accounts")
    op.drop_index("ix_model_accounts_workspace_account", table_name="model_accounts")
    op.drop_table("model_accounts")
