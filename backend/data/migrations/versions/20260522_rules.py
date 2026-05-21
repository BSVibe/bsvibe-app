"""Bundle 1.5a — routing_rules + rule_conditions, per-account.

Revision ID: bundle1_5a_rules
Revises: bundle1_initial
Create Date: 2026-05-22
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "bundle1_5a_rules"
down_revision: Union[str, Sequence[str], None] = "bundle1_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
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
        # DEFERRABLE INITIALLY DEFERRED lets reorder_rules issue many
        # priority updates in a single transaction (constraint checked
        # at COMMIT). Required by `RulesRepository.reorder_rules`.
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
    # First-match ordering scan.
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


def downgrade() -> None:
    op.drop_index("ix_rule_conditions_rule_id", table_name="rule_conditions")
    op.drop_table("rule_conditions")
    op.drop_index("ix_routing_rules_acct_priority", table_name="routing_rules")
    op.drop_index("ix_routing_rules_account_id", table_name="routing_rules")
    op.drop_index("ix_routing_rules_workspace_id", table_name="routing_rules")
    op.drop_table("routing_rules")
