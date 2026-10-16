"""workspaces.plan — which plan a workspace is on (#928)

The capacity levers (``max_concurrent_runs``, ``verify_stack_slots``) already live on the
workspace; this names the set. Every existing workspace is born ``free`` by the column default,
and a workspace already taken off the run cap (``max_concurrent_runs IS NULL`` — the operator's,
by ``workspace_run_cap``) is labelled ``operator``. No limit column is rewritten: the label
describes what each workspace already has.

Revision ID: workspace_plan
Revises: unattributed_llm_usage
Create Date: 2026-10-16
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

from backend.identity.workspaces_db import DEFAULT_PLAN

revision: str = "workspace_plan"
down_revision: Union[str, Sequence[str], None] = "unattributed_llm_usage"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "workspaces",
        sa.Column("plan", sa.String(16), nullable=False, server_default=DEFAULT_PLAN),
    )
    op.execute(sa.text("UPDATE workspaces SET plan = 'operator' WHERE max_concurrent_runs IS NULL"))


def downgrade() -> None:
    op.drop_column("workspaces", "plan")
