"""product_resources — per-product named pointers (repo / doc / deploy / note).

A workspace-scoped child of ``products``: one row per resource a product
works with. ``workspace_id`` carries the multi-tenancy axis (matching the
parent product so the global ORM auto-filter engages); ``product_id`` FKs the
parent with ``ON DELETE CASCADE`` so a product's resources go with it. ``kind``
is a short free-string chip; ``url`` and ``note`` are optional.

Revision ID: product_resources
Revises: model_account_nullable_key
Create Date: 2026-06-07
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "product_resources"
down_revision: Union[str, Sequence[str], None] = "model_account_nullable_key"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "product_resources",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("product_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("url", sa.String(2048), nullable=True),
        sa.Column("note", sa.String(2048), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_product_resources_workspace_id", "product_resources", ["workspace_id"])
    op.create_index("ix_product_resources_product_id", "product_resources", ["product_id"])


def downgrade() -> None:
    op.drop_index("ix_product_resources_product_id", table_name="product_resources")
    op.drop_index("ix_product_resources_workspace_id", table_name="product_resources")
    op.drop_table("product_resources")
