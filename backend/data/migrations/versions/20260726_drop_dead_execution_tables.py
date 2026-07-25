"""drop dead composition_snapshots + decomposer_steps + executor_tasks.artifact_refs.

Three schema objects a static audit confirmed have ZERO code producers or
consumers (prod tables are EMPTY):

* ``composition_snapshots`` (Bundle X) — a frozen prompt-template composition
  table that nothing ever wrote or read.
* ``decomposer_steps`` (Bundle X) — a CoT-decomposer step table that nothing
  ever wrote or read.
* ``executor_tasks.artifact_refs`` — an orphaned column. Its writer
  (``_persist_task_files``) was deleted (see ``record_result`` in
  ``backend.executors.dispatch``); the run's ``artifact_refs`` now come from the
  work tools themselves, never from this column. It was always NULL in prod.

Only the two Bundle-X tables' own indexes ride along; no other table FKs into
them, so the DROPs are clean. The downgrade recreates all three objects exactly
as the originals declared them (Bundle X ``20260525_bundle_x_execution`` +
``20260608_executor_artifact_capture``), matching types/nullability/indexes so
the round-trip is lossless (empty tables carry no data to restore).

Revision ID: drop_dead_execution_tables
Revises: product_metadata
Create Date: 2026-07-26
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "drop_dead_execution_tables"
down_revision: Union[str, Sequence[str], None] = "product_metadata"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Dropping a table drops its indexes with it; no other table FKs into these.
    op.drop_table("composition_snapshots")
    op.drop_table("decomposer_steps")
    op.drop_column("executor_tasks", "artifact_refs")


def downgrade() -> None:
    # Mirror executor_artifact_capture (20260608): re-add the orphaned column.
    op.add_column(
        "executor_tasks",
        sa.Column("artifact_refs", sa.JSON(), nullable=True),
    )

    # Mirror Bundle X (20260525): recreate decomposer_steps + its indexes.
    op.create_table(
        "decomposer_steps",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("execution_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("order_idx", sa.Integer(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_decomposer_steps_workspace_id", "decomposer_steps", ["workspace_id"])
    op.create_index("ix_decomposer_steps_run_order", "decomposer_steps", ["run_id", "order_idx"])

    # Mirror Bundle X (20260525): recreate composition_snapshots + its indexes.
    op.create_table(
        "composition_snapshots",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("execution_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("composition", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_composition_snapshots_workspace_id", "composition_snapshots", ["workspace_id"]
    )
    op.create_index("ix_composition_snapshots_run", "composition_snapshots", ["run_id"])
