"""executor_task_repo_url — per-task repo URL for coding-agent dispatch.

Lift E32. Adds one nullable string column to ``executor_tasks`` so the
backend can tell the worker "this task is a coding-agent invocation;
clone this repo into the per-task sandbox before running the executor".

Without this, the worker spins up a fresh ``tempfile.mkdtemp()`` per
task and hands the coding agent an empty directory; the agent has no
files to read or edit. The E31 dogfood (run 500ba446, 2026-06-15)
proved this: 6 cycles, ``success=True`` on every executor task, ``git
status --short`` empty, ``artifact_refs`` all NULL.

* ``repo_url`` (``VARCHAR(1024)``, nullable, no default) — git URL the
  worker should clone (``--depth 1`` of the default branch) into the
  per-task workspace before invoking the executor. NULL on every legacy
  row and a back-compat signal: chat-shaped callers (frame / judge /
  knowledge.ingest) keep the empty-tempdir path.

Safe to run online — column is nullable, no backfill needed. Down
migration drops it cleanly.

Revision ID: executor_task_repo_url
Revises: executor_task_model
Create Date: 2026-06-15
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "executor_task_repo_url"
down_revision: Union[str, Sequence[str], None] = "executor_task_model"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "executor_tasks",
        sa.Column("repo_url", sa.String(length=1024), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("executor_tasks", "repo_url")
