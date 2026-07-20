"""product_metadata — free-form ``metadata`` JSON slot on products.

The founder deliberately did NOT build a rigid product-lifecycle ENUM ("생애
주기는 제품마다 상이하니 의도적으로 안만든거야 — 지식그래프 등으로 대체 가능"). So
each product carries its own open ``metadata`` object instead: lifecycle stage,
custom attributes, or any context that agents + schedules + the founder read
and write.

* ``metadata`` (``JSON`` on SQLite, ``JSONB`` on PostgreSQL, NOT NULL, server
  default ``'{}'``) — a free-form JSON object. The server default backfills
  every legacy row to ``{}`` in a single statement so the column can be NOT
  NULL without a data-migration pass.

The SQLAlchemy model maps this column to the ``product_metadata`` attribute
(the ``metadata`` attribute name is reserved by the declarative base for its
``MetaData``); the DB column + the REST/MCP wire field stay ``metadata``.

Additive + reversible: the downgrade drops the column.

Revision ID: product_metadata
Revises: workspace_schedules_instruction
Create Date: 2026-07-23
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "product_metadata"
down_revision: Union[str, Sequence[str], None] = "workspace_schedules_instruction"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name
    if dialect == "postgresql":
        op.add_column(
            "products",
            sa.Column(
                "metadata",
                postgresql.JSONB(astext_type=sa.Text()),
                nullable=False,
                server_default=sa.text("'{}'"),
            ),
        )
    else:
        # SQLite test tier — plain JSON maps to TEXT; the model declares ``JSON``
        # so the same write path works against both dialects.
        op.add_column(
            "products",
            sa.Column(
                "metadata",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'{}'"),
            ),
        )


def downgrade() -> None:
    op.drop_column("products", "metadata")
