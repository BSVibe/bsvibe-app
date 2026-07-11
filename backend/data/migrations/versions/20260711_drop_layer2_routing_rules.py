"""Unified routing Lift 2 — drop legacy Layer-2 model-routing tables.

The litellm-hook model-routing engine (``backend.router.rules.*``) + its
``/api/v1/rules`` surface + the ``bsvibe_routing_rules_*`` MCP tools were hard-
deleted: they were never wired into dispatch (no global litellm callback), so
these two tables held only skeleton/seed data. Run-routing (``run_routing_rules``)
is the single surviving routing layer.

Revision ID: drop_layer2_routing_rules
Revises: workspace_language
Create Date: 2026-07-11
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "drop_layer2_routing_rules"
down_revision: Union[str, Sequence[str], None] = "workspace_language"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Children first (rule_conditions.rule_id -> routing_rules.id CASCADE).
    op.drop_index("ix_rule_conditions_rule_id", table_name="rule_conditions")
    op.drop_table("rule_conditions")
    op.drop_index("ix_routing_rules_acct_priority", table_name="routing_rules")
    op.drop_index("ix_routing_rules_account_id", table_name="routing_rules")
    op.drop_index("ix_routing_rules_workspace_id", table_name="routing_rules")
    op.drop_table("routing_rules")


def downgrade() -> None:
    # Recreate the Layer-2 tables exactly as bundle1_5a_rules did, so the
    # migration is reversible even though the ORM models are gone.
    op.create_table(
        "routing_rules",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("target_model", sa.String(200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workspace_id",
            "account_id",
            "name",
            name="uq_routing_rules_acct_name",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "account_id",
            "priority",
            name="uq_routing_rules_acct_priority",
            deferrable=True,
            initially="DEFERRED",
        ),
    )
    op.create_index("ix_routing_rules_workspace_id", "routing_rules", ["workspace_id"])
    op.create_index("ix_routing_rules_account_id", "routing_rules", ["account_id"])
    op.create_index(
        "ix_routing_rules_acct_priority",
        "routing_rules",
        ["workspace_id", "account_id", "priority"],
    )

    op.create_table(
        "rule_conditions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("rule_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("condition_type", sa.String(40), nullable=False),
        sa.Column("operator", sa.String(20), nullable=False),
        sa.Column("field", sa.String(60), nullable=False),
        sa.Column("value", postgresql.JSONB(), nullable=False),
        sa.Column("negate", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["rule_id"], ["routing_rules.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_rule_conditions_rule_id", "rule_conditions", ["rule_id"])
