"""Paddle subscription binding on workspaces + the webhook retry guard (#928 step 2)

Four nullable columns on ``workspaces`` (customer, subscription — unique, status, last event
time) and ``billing_webhook_events`` (event id → seen). Additive; no row is rewritten.

Revision ID: workspace_billing
Revises: workspace_plan
Create Date: 2026-10-16
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "workspace_billing"
down_revision: Union[str, Sequence[str], None] = "workspace_plan"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("workspaces", sa.Column("billing_customer_id", sa.String(64), nullable=True))
    op.add_column("workspaces", sa.Column("billing_subscription_id", sa.String(64), nullable=True))
    op.add_column("workspaces", sa.Column("billing_status", sa.String(32), nullable=True))
    op.add_column(
        "workspaces", sa.Column("billing_event_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_unique_constraint(
        "uq_workspaces_billing_subscription_id", "workspaces", ["billing_subscription_id"]
    )
    op.create_table(
        "billing_webhook_events",
        sa.Column("event_id", sa.String(64), primary_key=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("billing_webhook_events")
    op.drop_constraint("uq_workspaces_billing_subscription_id", "workspaces", type_="unique")
    op.drop_column("workspaces", "billing_event_at")
    op.drop_column("workspaces", "billing_status")
    op.drop_column("workspaces", "billing_subscription_id")
    op.drop_column("workspaces", "billing_customer_id")
