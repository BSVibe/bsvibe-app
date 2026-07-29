"""github_merge_watch — add nullable conflict_head_sha (PR7).

Closes the conflict-resolution loop: the worker stores the PR head SHA it last
re-dispatched a conflict on, so a ``needs_resolution`` re-poll can tell an
agent that has NOT re-pushed yet (same head → keep waiting, no re-dispatch)
from one that produced a new state (changed head → re-run the freshness merge;
clean → merge, still-conflict → re-dispatch once for the new head). Additive
schema only: one nullable text column, no backfill — safe to run online. The
down migration drops the column cleanly.

Revision ID: merge_watch_conflict_head_sha
Revises: github_merge_watch
Create Date: 2026-07-29
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "merge_watch_conflict_head_sha"
down_revision: Union[str, Sequence[str], None] = "github_merge_watch"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "github_merge_watch",
        sa.Column("conflict_head_sha", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("github_merge_watch", "conflict_head_sha")
