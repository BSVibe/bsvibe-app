"""workspace_schedules — durable schedule rows for the M1 schedule runner.

Workflow §12.5 #8 (Bundle G — Intake / Triggers) carry-over (Status §5
medium-term M1). ``backend/intake/schedule.py`` emits a TriggerEvent from a
fired schedule but nothing in prod fired it on a real interval — there was
no row whose ``next_run_at`` a runner could poll. This migration adds that
row.

One row per ``(workspace, plugin_name, cron_expr)``; the runner
(:class:`~backend.workers.schedule_runner.ScheduleWorker`) DB-polls
``enabled=True AND next_run_at <= now``, fires the emitter, and advances
``next_run_at`` via the :class:`~backend.workers.schedule_runner.ScheduleAdvancer`
seam. The (enabled, next_run_at) composite index keeps the polling SELECT
on an index scan as the table grows past a few hundred rows.

Revision ID: workspace_schedules
Revises: safe_mode_lifecycle
Create Date: 2026-05-25
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "workspace_schedules"
down_revision: Union[str, Sequence[str], None] = "safe_mode_lifecycle"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "workspace_schedules",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("product_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("plugin_name", sa.String(length=255), nullable=False),
        sa.Column("cron_expr", sa.String(length=255), nullable=False),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_fired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workspace_id",
            "plugin_name",
            "cron_expr",
            name="uq_workspace_schedules_ws_plugin_cron",
        ),
    )
    op.create_index(
        "ix_workspace_schedules_workspace_id",
        "workspace_schedules",
        ["workspace_id"],
    )
    op.create_index(
        "ix_workspace_schedules_product_id",
        "workspace_schedules",
        ["product_id"],
    )
    op.create_index(
        "ix_workspace_schedules_next_run_at",
        "workspace_schedules",
        ["next_run_at"],
    )
    op.create_index(
        "ix_workspace_schedules_due",
        "workspace_schedules",
        ["enabled", "next_run_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_workspace_schedules_due", table_name="workspace_schedules")
    op.drop_index("ix_workspace_schedules_next_run_at", table_name="workspace_schedules")
    op.drop_index("ix_workspace_schedules_product_id", table_name="workspace_schedules")
    op.drop_index("ix_workspace_schedules_workspace_id", table_name="workspace_schedules")
    op.drop_table("workspace_schedules")
