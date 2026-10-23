"""github_merge_watch: head_sha / ci_fix_attempts / ci_fix_head_sha

The CI deadline bounds how long ONE head may wait for its checks, so a head that
moved must restart the clock: ``head_sha`` remembers the head the poll last saw.
``ci_fix_attempts`` / ``ci_fix_head_sha`` bound how often red CI is sent back to
the run that opened the PR (and stop it being resent on a head the agent has not
yet replaced).

Revision ID: merge_watch_ci_fix
Revises: vault_documents
Create Date: 2026-10-23
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "merge_watch_ci_fix"
down_revision: Union[str, Sequence[str], None] = "vault_documents"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("github_merge_watch", sa.Column("head_sha", sa.String(64), nullable=True))
    op.add_column(
        "github_merge_watch",
        sa.Column("ci_fix_attempts", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("github_merge_watch", sa.Column("ci_fix_head_sha", sa.String(64), nullable=True))


def downgrade() -> None:
    op.drop_column("github_merge_watch", "ci_fix_head_sha")
    op.drop_column("github_merge_watch", "ci_fix_attempts")
    op.drop_column("github_merge_watch", "head_sha")
