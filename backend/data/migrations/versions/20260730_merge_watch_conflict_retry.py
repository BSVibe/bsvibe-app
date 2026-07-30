"""github_merge_watch — add conflict_attempts + conflict_dispatched_at.

Conflict-robustness: closes the "parked in ``needs_resolution`` forever" wedge.
The worker now bounds how long it waits for the agent to re-push a resolution
(``conflict_dispatched_at`` + a deadline), retries the re-dispatch a bounded
number of times (``conflict_attempts`` vs ``github_conflict_max_redispatch``),
and — when the retries are exhausted — escalates to a founder
``merge_conflict_review`` Decision instead of polling indefinitely.

Additive schema only:

* ``conflict_attempts`` — NOT NULL, server default ``0`` (so existing rows
  backfill to zero without a data migration).
* ``conflict_dispatched_at`` — nullable timestamptz (only set once a conflict
  has been dispatched). No backfill — a legacy dispatched row (``NULL``) is
  treated as "still within deadline" until its next fresh dispatch stamps it.

Safe to run online. The down migration drops both columns cleanly.

Revision ID: merge_watch_conflict_retry
Revises: merge_watch_conflict_head_sha
Create Date: 2026-07-30
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "merge_watch_conflict_retry"
down_revision: Union[str, Sequence[str], None] = "merge_watch_conflict_head_sha"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "github_merge_watch",
        sa.Column(
            "conflict_attempts",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "github_merge_watch",
        sa.Column(
            "conflict_dispatched_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("github_merge_watch", "conflict_dispatched_at")
    op.drop_column("github_merge_watch", "conflict_attempts")
