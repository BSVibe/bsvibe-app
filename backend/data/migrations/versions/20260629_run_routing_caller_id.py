"""run_routing_caller_id — Lift E2 caller_id column on run-routing rules.

Adds ``run_routing_rules.caller_id`` (VARCHAR(120) NULL). Lift E2's
:class:`~backend.dispatch.resolver.ModelAccountResolver` matches rules
on this column first; the legacy ``{"field": "caller_id", "operator":
"eq", "value": "..."}`` clause inside ``conditions`` remains honoured as
a back-compat shape so rows authored before the column existed keep
matching.

No backfill — every existing row stays ``caller_id = NULL``. Those
legacy rows no longer match any caller via the column path (because
caller_id is required for any non-default rule going forward); the
back-compat path in :func:`backend.dispatch.resolver._rule_matches_caller`
still honours their condition-clause shape so production traffic is
untouched. The default rule (``is_default = TRUE`` AND
``conditions = []`` AND ``caller_id IS NULL``) keeps catching unmatched
callers in any workspace that authored one.

Migration policy memo: founders who want their legacy rules promoted to
the canonical column shape edit them through PWA Settings → Models →
Routing rules after E2 ships. The PWA write path REQUIRES
``caller_id``; the read path surfaces the existing condition clause as
the source of the routed value so editing is a one-click promotion.

Reversible: ``downgrade`` drops the column.

Revision ID: run_routing_caller_id
Revises: workspace_default_account
Create Date: 2026-06-06
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "run_routing_caller_id"
down_revision: Union[str, Sequence[str], None] = "workspace_default_account"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "run_routing_rules",
        sa.Column("caller_id", sa.String(length=120), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("run_routing_rules", "caller_id")
