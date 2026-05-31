"""executor tasks — run binding + captured artifact refs (executor-pool B1).

Adds two nullable columns to ``executor_tasks`` so a dispatched executor task
can surface the files its CLI produced back to the run:

* ``run_id`` — the :class:`backend.workflow.infrastructure.db.ExecutionRun` the task belongs
  to. The :class:`backend.executors.orchestrator.ExecutorOrchestrator` sets it
  so the result path can resolve the run workspace
  (``run_workspace_root/<run_id>/``) to persist captured files into.
* ``artifact_refs`` — JSON list of the relative paths the backend accepted and
  persisted under the run workspace; surfaced as the verified Deliverable's
  ``artifact_refs`` (was always ``[]`` before B1).

Both are NULLABLE — no backfill needed; substrate-only tasks created before B1
(or without a run binding) simply skip file persistence. The downgrade drops
the columns + the ``run_id`` index.

Revision ID: executor_artifact_capture
Revises: product_resources
Create Date: 2026-06-08
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "executor_artifact_capture"
down_revision: Union[str, Sequence[str], None] = "product_resources"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "executor_tasks",
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "executor_tasks",
        sa.Column("artifact_refs", sa.JSON(), nullable=True),
    )
    op.create_index("ix_executor_tasks_run_id", "executor_tasks", ["run_id"])


def downgrade() -> None:
    op.drop_index("ix_executor_tasks_run_id", table_name="executor_tasks")
    op.drop_column("executor_tasks", "artifact_refs")
    op.drop_column("executor_tasks", "run_id")
