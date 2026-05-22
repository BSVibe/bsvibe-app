"""Bundle H — workspaces + products first-class entities.

Workflow §3. Adds the top-level Workspace + per-workspace Product tables;
existing tables already carry ``workspace_id`` UUID columns but had no FK
target until now. The migration is FK-free deliberately — adding the FKs
backfill-safely is a follow-up once the product entity is reachable from
prod data.

Revision ID: bundle_h_workspaces
Revises: bundle_g_glue
Create Date: 2026-05-27
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "bundle_h_workspaces"
down_revision: Union[str, Sequence[str], None] = "bundle_g_glue"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "workspaces",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("region", sa.String(length=32), nullable=False, server_default="us-1"),
        sa.Column("safe_mode", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "products",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "workspace_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("repo_url", sa.String(length=512), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("workspace_id", "slug", name="uq_products_ws_slug"),
    )
    op.create_index("ix_products_workspace_id", "products", ["workspace_id"])


def downgrade() -> None:
    op.drop_index("ix_products_workspace_id", table_name="products")
    op.drop_table("products")
    op.drop_table("workspaces")
