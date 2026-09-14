"""workspaces.monthly_token_budget — the per-workspace cumulative token quota

#930 part 1. The per-run ceiling (``agent_max_run_tokens``) bounds ONE run;
nothing bounded a workspace. Three concurrent runs, restarted forever, sits
inside ``max_concurrent_runs`` and is unbounded in tokens. #953 sealed the meter
leaks, so ``execution_runs.usage_*`` finally totals act + frame + verify + tick
and is worth budgeting against.

Shaped after ``workspace_run_cap`` deliberately: a nullable column with a DDL
default, ``NULL`` = unlimited, and the operator's own workspace taken off the
quota by an UPDATE. The grandfathering is the same argument — the operator's
workspace is the one running BSVibe's own development and would be the first
thing this locks out — and is a no-op on any deployment without an
``admin@bsvibe.dev`` account.

⚠️ It is a SAFETY quota, not a price lever. ``max_concurrent_runs`` is the free
plan's price lever and stays the only one; billing is #928.

Also adds ``ix_execution_runs_ws_created``. The budget's predicate is
``workspace_id = ? AND created_at >= <month start>`` and no existing index on
``execution_runs`` covers it — ``ix_execution_runs_ws_status`` is keyed on
``status`` and ``ix_execution_runs_ws_product`` on ``product_id``, so the read
would fall back to the bare ``workspace_id`` index and filter every run the
workspace ever made. It runs at every founder submission.

Revision ID: workspace_token_budget
Revises: executor_task_tokens (the FILENAME is ...token_usage.py)
Create Date: 2026-09-14
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

from backend.identity.workspaces_db import DEFAULT_MONTHLY_TOKEN_BUDGET

# ⚠ Revision ids are capped at 32 chars by ``alembic_version.version_num`` —
# a longer one fails at the UPDATE, not at the DDL. This is 22.
revision: str = "workspace_token_budget"
down_revision: Union[str, Sequence[str], None] = "executor_task_tokens"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: The account whose workspace operates the service. Matched by email so the
#: statement is a no-op wherever that user does not exist.
_OPERATOR_EMAIL = "admin@bsvibe.dev"


def upgrade() -> None:
    op.add_column(
        "workspaces",
        sa.Column(
            "monthly_token_budget",
            sa.BigInteger(),
            nullable=True,
            server_default=str(DEFAULT_MONTHLY_TOKEN_BUDGET),
        ),
    )
    op.execute(
        sa.text(
            """
            UPDATE workspaces SET monthly_token_budget = NULL
            WHERE id IN (
                SELECT m.workspace_id FROM memberships m
                JOIN users u ON u.id = m.user_id
                WHERE u.email = :operator_email
            )
            """
        ).bindparams(operator_email=_OPERATOR_EMAIL)
    )
    op.create_index(
        "ix_execution_runs_ws_created",
        "execution_runs",
        ["workspace_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_execution_runs_ws_created", table_name="execution_runs")
    op.drop_column("workspaces", "monthly_token_budget")
