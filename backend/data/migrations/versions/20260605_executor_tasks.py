"""executor tasks — dispatch substrate for the executor pool (Lift 2).

Adds the ``executor_tasks`` table that backs :class:`backend.executors.db.ExecutorTaskRow`
— the dispatch unit moved through pending → dispatched → done/failed by the
dispatch service (:mod:`backend.executors.dispatch`) and the poll/result
endpoints. Mirrors BSGateway's ``executor_tasks``, re-tenanted on
``workspace_id``.

Revision ID: executor_tasks
Revises: executor_workers
Create Date: 2026-06-05
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "executor_tasks"
down_revision: Union[str, Sequence[str], None] = "executor_workers"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "executor_tasks",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("worker_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("executor_type", sa.String(length=64), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("system", sa.Text(), nullable=False, server_default=""),
        sa.Column("workspace_dir", sa.String(length=1024), nullable=False, server_default="."),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("output", sa.Text(), nullable=False, server_default=""),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_executor_tasks_workspace_id", "executor_tasks", ["workspace_id"])
    op.create_index("ix_executor_tasks_worker_id", "executor_tasks", ["worker_id"])
    op.create_index("ix_executor_tasks_status", "executor_tasks", ["status"])


def downgrade() -> None:
    op.drop_index("ix_executor_tasks_status", table_name="executor_tasks")
    op.drop_index("ix_executor_tasks_worker_id", table_name="executor_tasks")
    op.drop_index("ix_executor_tasks_workspace_id", table_name="executor_tasks")
    op.drop_table("executor_tasks")
