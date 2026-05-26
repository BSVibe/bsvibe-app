"""resource_bindings — per-Product × Connector 3-knob binding (Workflow §3).

A Resource (in the spec's sense) is the binding that carries the founder-set
knobs for one Product against one ConnectorAccount + a connector-side
``resource_id``:

* ``selection`` — connector-shaped scope (default ``{}`` = the whole resource).
* ``trigger`` — ``{"enabled": bool, "filters": dict}`` (the *do I act* knob).
  Default = disabled with no filters.
* ``output_mode`` — ``'safe'`` (queue for founder approval, default) or
  ``'direct'`` (auto-deliver). TEXT + app-side validation (no Postgres ENUM)
  keeps SQLite test compatibility and dodges the alembic enum CREATE TYPE traps.

Workspace-scoped (``workspace_id`` for the global ORM auto-filter); parented to
``products`` and ``connector_accounts`` via FKs with ``ON DELETE CASCADE`` — a
product or account removal cascades to its bindings.

The ``ix_resource_bindings_lookup`` index on ``(connector_account_id,
resource_id)`` is what Receive (B10b) will hit to resolve an inbound webhook →
binding → Product.

Revision ID: resource_bindings
Revises: executor_artifact_capture
Create Date: 2026-06-09
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "resource_bindings"
down_revision: Union[str, Sequence[str], None] = "executor_artifact_capture"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "resource_bindings",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("product_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("connector_account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("resource_id", sa.String(512), nullable=False),
        sa.Column("selection", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column(
            "trigger",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("""'{"enabled": false, "filters": {}}'"""),
        ),
        sa.Column(
            "output_mode",
            sa.String(16),
            nullable=False,
            server_default=sa.text("'safe'"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["connector_account_id"], ["connector_accounts.id"], ondelete="CASCADE"
        ),
    )
    op.create_index("ix_resource_bindings_workspace_id", "resource_bindings", ["workspace_id"])
    op.create_index("ix_resource_bindings_product_id", "resource_bindings", ["product_id"])
    op.create_index(
        "ix_resource_bindings_lookup",
        "resource_bindings",
        ["connector_account_id", "resource_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_resource_bindings_lookup", table_name="resource_bindings")
    op.drop_index("ix_resource_bindings_product_id", table_name="resource_bindings")
    op.drop_index("ix_resource_bindings_workspace_id", table_name="resource_bindings")
    op.drop_table("resource_bindings")
