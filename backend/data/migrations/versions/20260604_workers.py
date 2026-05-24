"""executor workers — external executor-worker registration subsystem (Lift 1).

Adds the two tables that back :mod:`backend.executors.db`:

* ``executor_workers`` — one row per registered external CLI executor host
  (token-authed, ``workspace_id`` scoped, labels/capabilities JSON).
* ``executor_install_tokens`` — the single active install token per workspace
  (unique on ``workspace_id``; re-minting replaces it).

These are DISTINCT from the Bundle G ``workers`` / ``worker_install_tokens``
tables (``backend.workers.db``), which model the orchestrator's own internal
consumer-group daemons. Same ``workspace_id`` axis, different concept.

Revision ID: executor_workers
Revises: notification_prefs
Create Date: 2026-06-04
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "executor_workers"
down_revision: Union[str, Sequence[str], None] = "notification_prefs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "executor_workers",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("labels", sa.JSON(), nullable=False),
        sa.Column("capabilities", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="offline"),
        sa.Column("last_heartbeat", sa.DateTime(timezone=True), nullable=True),
        sa.Column("token_hash", sa.String(length=128), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_executor_workers_workspace_id", "executor_workers", ["workspace_id"])
    op.create_index("ix_executor_workers_token_hash", "executor_workers", ["token_hash"])

    op.create_table(
        "executor_install_tokens",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("token_hash", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", name="uq_executor_install_tokens_workspace"),
    )
    op.create_index(
        "ix_executor_install_tokens_workspace_id",
        "executor_install_tokens",
        ["workspace_id"],
    )
    op.create_index(
        "ix_executor_install_tokens_token_hash",
        "executor_install_tokens",
        ["token_hash"],
    )


def downgrade() -> None:
    op.drop_index("ix_executor_install_tokens_token_hash", table_name="executor_install_tokens")
    op.drop_index("ix_executor_install_tokens_workspace_id", table_name="executor_install_tokens")
    op.drop_table("executor_install_tokens")
    op.drop_index("ix_executor_workers_token_hash", table_name="executor_workers")
    op.drop_index("ix_executor_workers_workspace_id", table_name="executor_workers")
    op.drop_table("executor_workers")
