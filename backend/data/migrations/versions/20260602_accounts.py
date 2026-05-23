"""accounts — per-workspace personal billing account (the account axis).

One row per workspace's personal :class:`backend.accounts.account_models.Account`,
auto-seeded at login bootstrap. It is the partition key the model-accounts
surface scopes on via ``X-BSVibe-Account-Id``. NO unique constraint on
``workspace_id`` (room for future multi-account workspaces); resolution is
earliest-created-wins.

Revision ID: accounts
Revises: connector_delivery_config
Create Date: 2026-06-02
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "accounts"
down_revision: Union[str, Sequence[str], None] = "connector_delivery_config"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "accounts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("label", sa.String(128), nullable=False, server_default="personal"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_accounts_workspace_id", "accounts", ["workspace_id"])


def downgrade() -> None:
    op.drop_index("ix_accounts_workspace_id", table_name="accounts")
    op.drop_table("accounts")
