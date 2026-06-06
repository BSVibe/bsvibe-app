"""workspace_default_account — Lift E1 default-account fallback column.

Adds ``workspaces.default_account_id`` UUID NULL with a foreign key to
``model_accounts.id`` ON DELETE SET NULL. The column is the new
:class:`backend.dispatch.resolver.ModelAccountResolver`'s explicit
workspace-default fallback: when no :class:`RunRoutingRuleRow` matches
a caller_id, the resolver looks here. A founder sets the column through
Settings → Models or the MCP tool ``bsvibe_workspace_set_default_account``;
BSVibe NEVER auto-stamps it (founder policy ``bsvibe-no-implicit-routing``).

No backfill — every existing row stays ``NULL``, which is the
architecturally correct default (the resolver raises
:class:`~backend.dispatch.resolver.NoMatchingRouteError` when both rules
and the column are absent, instead of silently picking a model).

Reversible: ``downgrade`` drops the FK and the column.

Revision ID: workspace_default_account
Revises: connector_oauth_app_credentials
Create Date: 2026-06-06
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "workspace_default_account"
down_revision: Union[str, Sequence[str], None] = "connector_oauth_app_credentials"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "workspaces",
        sa.Column(
            "default_account_id",
            sa.Uuid(),
            nullable=True,
        ),
    )
    # Named FK so downgrade can drop it explicitly. ``ON DELETE SET NULL``
    # so removing the model account leaves the workspace row intact and
    # the resolver falls back to ``NoMatchingRouteError`` (the founder is
    # prompted to pick a new default), never a dangling FK / 500.
    op.create_foreign_key(
        "fk_workspaces_default_account_id",
        "workspaces",
        "model_accounts",
        ["default_account_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_workspaces_default_account_id", "workspaces", type_="foreignkey")
    op.drop_column("workspaces", "default_account_id")
