"""One pull request, one merge-watch row.

``(repo, pr_number)`` identifies a pull request permanently — GitHub never
reuses a number within a repo — so a second row for the same PR is always a
duplicate. Live: run ``e53e9b5c`` delivered twice, each delivery found the same
open PR #754, and two rows were created six seconds apart. That doubled the
GitHub polling, put two racers on the same per-repo merge lock, and (since #746
taught the watch to speak when it gives up) told the founder the same thing
twice.

The application-side check in ``GithubMergeWatchRepository.add`` makes the
duplicate a calm skip; this index is what makes it impossible, including for two
deliveries racing in separate transactions.

Existing duplicates are folded first, keeping the row that has travelled
furthest (the oldest — it has the poll history and any conflict bookkeeping).
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "one_pr_one_watch"
down_revision: Union[str, Sequence[str], None] = "safe_mode_decision_reason"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_INDEX = "uq_github_merge_watch_repo_pr"


def upgrade() -> None:
    # Fold existing duplicates onto the oldest row per (repo, pr_number). The
    # index cannot be created while they exist, and the oldest is the one whose
    # attempts / conflict state describe what actually happened.
    op.execute(
        sa.text(
            """
            DELETE FROM github_merge_watch a
             USING github_merge_watch b
             WHERE a.repo = b.repo
               AND a.pr_number = b.pr_number
               AND (a.created_at, a.id) > (b.created_at, b.id)
            """
        )
    )
    op.create_index(_INDEX, "github_merge_watch", ["repo", "pr_number"], unique=True)


def downgrade() -> None:
    op.drop_index(_INDEX, table_name="github_merge_watch")
