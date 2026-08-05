"""executor_tasks — ``execution_target`` (#692, client-attach execution).

Adds WHERE/HOW a task's pure worker executes, derived from the product:

* ``execution_target`` (VARCHAR(32), NOT NULL, server default
  ``'server_sandbox'``) — ``server_sandbox`` (today's model: file/shell route
  through the MCP work tools into the server-side sandbox) or ``client_attach``
  (native execution in the user's own working directory).

A plain String, NOT a PG enum — the vocabulary lives in
``backend.workflow.domain.execution_target``, so no CREATE TYPE / ALTER TYPE
migration hazards. Additive + reversible; every existing row backfills to the
safe ``server_sandbox`` default.

Revision ID: executor_task_execution_target
Revises: merge_watch_conflict_retry
Create Date: 2026-08-05
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "executor_task_execution_target"
down_revision: Union[str, Sequence[str], None] = "merge_watch_conflict_retry"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "executor_tasks",
        sa.Column(
            "execution_target",
            sa.String(length=32),
            nullable=False,
            server_default="server_sandbox",
        ),
    )


def downgrade() -> None:
    op.drop_column("executor_tasks", "execution_target")
