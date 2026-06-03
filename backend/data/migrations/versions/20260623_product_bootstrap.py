"""product_bootstrap — bootstrap_status + telemetry columns on products.

Lift A v2. Adds four columns to ``products`` so a freshly-created product
that carries a ``repo_url`` can carry its background-bootstrap progress
visibly:

* ``bootstrap_status`` (``String(40)``, nullable) — short lifecycle marker.
  v1 vocabulary: ``pending`` / ``cloning`` / ``analyzing`` / ``ingesting`` /
  ``complete`` / ``failed:clone`` / ``failed:too_large`` / ``failed:ingest``.
* ``bootstrap_run_id`` (UUID, nullable) — loose correlation id for the
  background job (logging / observability), not an FK.
* ``bootstrap_artifacts_count`` (INTEGER, nullable) — count of artifacts
  handed to :class:`Knowledge.ingest` on success.
* ``bootstrap_error`` (TEXT, nullable) — short human-legible reason on
  failure (one line; full traceback stays in structured logs).

Plus an index on ``(workspace_id, bootstrap_status)`` so the founder's
"products currently being bootstrapped" lookup stays cheap as workspaces
grow.

No backfill needed — existing rows already have NULLs; the founder UI
reads "no bootstrap row" as "no bootstrap was run" and renders nothing.

Revision ID: product_bootstrap
Revises: connector_last_import
Create Date: 2026-06-03
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "product_bootstrap"
down_revision: Union[str, Sequence[str], None] = "connector_last_import"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "products",
        sa.Column("bootstrap_status", sa.String(length=40), nullable=True),
    )
    op.add_column(
        "products",
        sa.Column("bootstrap_run_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "products",
        sa.Column("bootstrap_artifacts_count", sa.Integer(), nullable=True),
    )
    op.add_column(
        "products",
        sa.Column("bootstrap_error", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_products_ws_bootstrap_status",
        "products",
        ["workspace_id", "bootstrap_status"],
    )


def downgrade() -> None:
    op.drop_index("ix_products_ws_bootstrap_status", table_name="products")
    op.drop_column("products", "bootstrap_error")
    op.drop_column("products", "bootstrap_artifacts_count")
    op.drop_column("products", "bootstrap_run_id")
    op.drop_column("products", "bootstrap_status")
