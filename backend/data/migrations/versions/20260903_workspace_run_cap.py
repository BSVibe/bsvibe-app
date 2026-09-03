"""workspaces.max_concurrent_runs — the free plan's concurrent-run budget

How many runs a workspace may HOLD at once, ``review_ready`` included. It lives
per workspace, beside ``verify_stack_slots``, for the same stated reason that
one does: a plan tier is not a server constant.

``NULL`` = uncapped. Existing workspaces are backfilled to the free default by
the column's ``server_default``, and the operator's own workspace is then taken
off the plan — it holds 30 runs awaiting review and would otherwise be locked
out by its own price lever the moment this deploys. That grandfathering is a
no-op on any deployment without an ``admin@bsvibe.dev`` account.

Revision ID: workspace_run_cap
Revises: flatten_notification_matrix
Create Date: 2026-09-03
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

from backend.identity.workspaces_db import DEFAULT_MAX_CONCURRENT_RUNS

revision: str = "workspace_run_cap"
down_revision: Union[str, Sequence[str], None] = "flatten_notification_matrix"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: The account whose workspace operates the service. Matched by email so the
#: statement is a no-op wherever that user does not exist.
_OPERATOR_EMAIL = "admin@bsvibe.dev"


def upgrade() -> None:
    op.add_column(
        "workspaces",
        sa.Column(
            "max_concurrent_runs",
            sa.Integer(),
            nullable=True,
            server_default=str(DEFAULT_MAX_CONCURRENT_RUNS),
        ),
    )
    op.execute(
        sa.text(
            """
            UPDATE workspaces SET max_concurrent_runs = NULL
            WHERE id IN (
                SELECT m.workspace_id FROM memberships m
                JOIN users u ON u.id = m.user_id
                WHERE u.email = :operator_email
            )
            """
        ).bindparams(operator_email=_OPERATOR_EMAIL)
    )


def downgrade() -> None:
    op.drop_column("workspaces", "max_concurrent_runs")
