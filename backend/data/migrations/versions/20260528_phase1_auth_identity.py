"""Phase 1 auth — users + memberships identity tables.

Workflow §3 / §10.1. Adds the first-class ``User`` keyed to its Supabase
identity and the ``Membership`` join to ``Workspace``. FKs target ``users``
and ``workspaces`` (both present after ``bundle_h_workspaces``). v1 operates
1:1 with ``role='owner'``; the schema is N:M-ready (``invited_by_user_id`` /
``left_at``) so team workspaces ship without another migration.

Revision ID: phase1_auth_identity
Revises: bundle_h_workspaces
Create Date: 2026-05-28
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "phase1_auth_identity"
down_revision: Union[str, Sequence[str], None] = "bundle_h_workspaces"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("supabase_user_id", sa.String(length=255), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("supabase_user_id", name="uq_users_supabase_user_id"),
    )
    op.create_index("ix_users_supabase_user_id", "users", ["supabase_user_id"])

    op.create_table(
        "memberships",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "workspace_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", sa.String(length=16), nullable=False, server_default="owner"),
        sa.Column("invited_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("joined_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("left_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("user_id", "workspace_id", name="uq_memberships_user_ws"),
    )
    op.create_index("ix_memberships_user_id", "memberships", ["user_id"])
    op.create_index("ix_memberships_workspace_id", "memberships", ["workspace_id"])


def downgrade() -> None:
    op.drop_index("ix_memberships_workspace_id", table_name="memberships")
    op.drop_index("ix_memberships_user_id", table_name="memberships")
    op.drop_table("memberships")
    op.drop_index("ix_users_supabase_user_id", table_name="users")
    op.drop_table("users")
