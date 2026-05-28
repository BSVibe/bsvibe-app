"""run_routing_rules — Phase 1 rule-based RUN routing.

A per-workspace, priority-ordered rule set that selects WHICH ModelAccount
(native vs executor CLI) drives a run, keyed on the run's framed signals.
Distinct from the gateway's account-scoped chat/model ``routing_rules`` table
(which picks the model WITHIN a native run via the litellm hook). Empty for
existing workspaces → resolution falls back to the legacy single-active policy.

Revision ID: run_routing_rules
Revises: w1_workspace_cleanup
Create Date: 2026-05-28
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "run_routing_rules"
down_revision: Union[str, Sequence[str], None] = "w1_workspace_cleanup"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "run_routing_rules",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("is_default", sa.Boolean(), nullable=False),
        sa.Column("target", sa.String(length=255), nullable=False),
        sa.Column("conditions", sa.JSON(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "name", name="uq_run_routing_rule_name"),
    )
    op.create_index("ix_run_routing_rules_workspace_id", "run_routing_rules", ["workspace_id"])


def downgrade() -> None:
    op.drop_index("ix_run_routing_rules_workspace_id", table_name="run_routing_rules")
    op.drop_table("run_routing_rules")
