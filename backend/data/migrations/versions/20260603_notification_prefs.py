"""notification_prefs — per-workspace notification preferences.

One row per workspace holding the events x channels enable matrix (JSON,
keyed ``event_id -> channel_id -> bool``) plus a quiet-hours window
(``HH:MM`` strings). Backs the Settings -> Notifications surface. v1 stores
the PREFERENCES only; real email/Slack delivery wiring is a later phase.

Revision ID: notification_prefs
Revises: accounts
Create Date: 2026-06-03
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "notification_prefs"
down_revision: Union[str, Sequence[str], None] = "accounts"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "notification_prefs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("matrix", sa.JSON(), nullable=False),
        sa.Column("quiet_hours_enabled", sa.Boolean(), nullable=False),
        sa.Column("quiet_hours_start", sa.String(length=5), nullable=False),
        sa.Column("quiet_hours_end", sa.String(length=5), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", name="uq_notification_prefs_workspace_id"),
    )
    op.create_index("ix_notification_prefs_workspace_id", "notification_prefs", ["workspace_id"])


def downgrade() -> None:
    op.drop_index("ix_notification_prefs_workspace_id", table_name="notification_prefs")
    op.drop_table("notification_prefs")
