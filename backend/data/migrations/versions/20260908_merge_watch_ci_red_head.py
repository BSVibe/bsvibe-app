"""github_merge_watch.ci_red_head_sha — which head a failing check was seen on

A red check is decided about that ATTEMPT, not about the commit. GitHub re-runs
REPLACE a check-run's conclusion, and the check-runs endpoint answers with the
latest attempt per name — so a re-run of the same commit simply reads green.

The watch used to treat the first red as final: row FAILED, watching over.
``failed`` is not a claimable status, so nothing ever looked again. Live PR #892
(2026-09-07): ``lint-and-test`` red at 07:00, green on a re-run of the same
commit at 07:25, and the PR sat open until it was merged by hand.

This column remembers WHICH head was red, so the second look on the same head
is what calls the founder, and an agent re-push starts the grace over — the same
way ``conflict_head_sha`` invalidates a stale conflict. NULL until a red is seen.

Revision ID: merge_watch_ci_red_head
Revises: workspace_run_cap
Create Date: 2026-09-08
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "merge_watch_ci_red_head"
down_revision: Union[str, Sequence[str], None] = "workspace_run_cap"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "github_merge_watch",
        sa.Column("ci_red_head_sha", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("github_merge_watch", "ci_red_head_sha")
