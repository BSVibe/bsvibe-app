"""executor_workers.claude_auth_failed_at — a worker that cannot sign in to Claude

Measured 2026-10-15: the host worker's Claude OAuth refresh died (``invalid_grant``) and every
agent turn failed for ~17 h with only a log line to show for it. The worker now reports the
failure on its heartbeat; this column remembers it so the founder is alerted once on the way
down and once on the way back up. Nullable, additive.

Revision ID: worker_claude_auth_failed
Revises: workspace_billing
Create Date: 2026-10-16
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "worker_claude_auth_failed"
down_revision: Union[str, Sequence[str], None] = "workspace_billing"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "executor_workers",
        sa.Column("claude_auth_failed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("executor_workers", "claude_auth_failed_at")
