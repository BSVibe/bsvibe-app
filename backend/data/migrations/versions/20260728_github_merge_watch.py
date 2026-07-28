"""github_merge_watch — durable auto-merge poll queue (PR3).

One row per opened PR eligible for CI-green auto-merge. Additive schema only:
a new table + its ``github_merge_watch_status_enum`` Postgres enum. Mirrors the
delivery-table conventions (Bundle G): ``postgresql.ENUM(create_type=False)``
for the status column with a pre-created named type, loose UUID ``run_id`` /
``deliverable_id`` (NO cross-domain FK — exactly as ``delivery_events`` leaves
``deliverable_id`` loose), and named indexes. Safe to run online — a brand-new
empty table, no backfill. Down migration drops the table + enum cleanly.

Revision ID: github_merge_watch
Revises: run_claim_columns
Create Date: 2026-07-28
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "github_merge_watch"
down_revision: Union[str, Sequence[str], None] = "run_claim_columns"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_MERGE_WATCH_STATUS_VALUES = (
    "pending_ci",
    "merging",
    "merged",
    "failed",
    "needs_resolution",
    "abandoned",
)

_MERGE_WATCH_STATUS = postgresql.ENUM(
    *_MERGE_WATCH_STATUS_VALUES, name="github_merge_watch_status_enum", create_type=False
)


def upgrade() -> None:
    bind = op.get_bind()
    # Pre-create the named enum (checkfirst — idempotent); the column below
    # references it with create_type=False so DDL never emits CREATE TYPE twice.
    sa.Enum(*_MERGE_WATCH_STATUS_VALUES, name="github_merge_watch_status_enum").create(
        bind, checkfirst=True
    )

    op.create_table(
        "github_merge_watch",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("deliverable_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("repo", sa.String(length=255), nullable=False),
        sa.Column("pr_number", sa.Integer(), nullable=False),
        sa.Column("branch", sa.String(length=255), nullable=False),
        sa.Column("base_branch", sa.String(length=255), nullable=False, server_default="main"),
        sa.Column("status", _MERGE_WATCH_STATUS, nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_poll_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("conflict_dispatched", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_github_merge_watch_workspace_id", "github_merge_watch", ["workspace_id"])
    op.create_index(
        "ix_github_merge_watch_status_next_poll",
        "github_merge_watch",
        ["status", "next_poll_at"],
    )
    op.create_index("ix_github_merge_watch_repo", "github_merge_watch", ["repo"])
    op.create_index("ix_github_merge_watch_run", "github_merge_watch", ["run_id"])


def downgrade() -> None:
    op.drop_index("ix_github_merge_watch_run", table_name="github_merge_watch")
    op.drop_index("ix_github_merge_watch_repo", table_name="github_merge_watch")
    op.drop_index("ix_github_merge_watch_status_next_poll", table_name="github_merge_watch")
    op.drop_index("ix_github_merge_watch_workspace_id", table_name="github_merge_watch")
    op.drop_table("github_merge_watch")
    bind = op.get_bind()
    sa.Enum(name="github_merge_watch_status_enum").drop(bind, checkfirst=True)
