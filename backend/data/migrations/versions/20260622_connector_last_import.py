"""connector_last_import — surface last-import timestamp + count on connectors.

Lift B. Adds two columns to ``connector_accounts`` so the founder UI can
show "last imported at" / "last imported count" on every inbound binding
(Obsidian, Claude, GPT, Notion). Both columns are nullable — a connector
that has never been imported has no last value; the
:func:`POST /api/v1/connectors/{id}/import` endpoint writes them on each
successful run.

No backfill needed — existing rows simply read ``NULL`` until their next
import.

Revision ID: connector_last_import
Revises: ontology_corrections
Create Date: 2026-06-03
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "connector_last_import"
down_revision: Union[str, Sequence[str], None] = "ontology_corrections"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "connector_accounts",
        sa.Column("last_import_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "connector_accounts",
        sa.Column("last_import_count", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("connector_accounts", "last_import_count")
    op.drop_column("connector_accounts", "last_import_at")
