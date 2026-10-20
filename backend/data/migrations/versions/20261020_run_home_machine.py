"""execution_runs.home_machine — which machine drives a claimed run (#929)

``claimed_by`` is a random id per worker PROCESS. With more than one machine, the MCP work
tools and a run arriving on a new machine need to know which MACHINE holds the run's worktree
and sandbox. Stamped with ``BSVIBE_MACHINE_ID`` on claim, cleared with the claim. Nullable,
additive: NULL on a single-box deployment and for unclaimed runs.

Revision ID: run_home_machine
Revises: deliverable_delivered_at
Create Date: 2026-10-20
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "run_home_machine"
down_revision: Union[str, Sequence[str], None] = "deliverable_delivered_at"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("execution_runs", sa.Column("home_machine", sa.String(128), nullable=True))


def downgrade() -> None:
    op.drop_column("execution_runs", "home_machine")
