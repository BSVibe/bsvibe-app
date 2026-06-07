"""product_bootstrap_progress — per-chunk progress JSON on products.

Lift E9 Part B. Adds one nullable column to ``products`` so the
``product_bootstrap_runtime`` can surface incremental per-chunk progress
onto the row while a bootstrap is in flight, instead of the founder
polling ``bsvibe_products_show`` and seeing the same opaque
``status="ingesting"`` for an hour with no signal of forward motion.

* ``bootstrap_progress`` (``JSON`` on SQLite, ``JSONB`` on PostgreSQL,
  nullable) — small dict ``{"chunks_done", "chunks_total",
  "chunks_failed", "notes_created", "notes_updated", "phase"}``. Written
  by a subscriber on the ingest event bus (``INGEST_COMPILE_BATCH_*``).
  ``NULL`` on every legacy row and on every new product whose bootstrap
  hasn't reached the compile_batch stage yet — the founder UI treats
  ``NULL`` as "no incremental signal, fall back to the status pill".

Safe to run online — column is nullable with no default backfill needed.

Revision ID: product_bootstrap_progress
Revises: drop_executor_install_tokens
Create Date: 2026-06-07
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "product_bootstrap_progress"
down_revision: Union[str, Sequence[str], None] = "drop_executor_install_tokens"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name
    if dialect == "postgresql":
        op.add_column(
            "products",
            sa.Column(
                "bootstrap_progress",
                postgresql.JSONB(astext_type=sa.Text()),
                nullable=True,
            ),
        )
    else:
        # SQLite test tier — plain JSON column type maps to TEXT under the
        # hood; the SQLAlchemy model declares ``JSON`` so the same write
        # path works against both dialects.
        op.add_column(
            "products",
            sa.Column("bootstrap_progress", sa.JSON(), nullable=True),
        )


def downgrade() -> None:
    op.drop_column("products", "bootstrap_progress")
