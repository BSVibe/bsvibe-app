"""connector_delivery_config — outbound delivery target binding (§12.5 #8).

A verified Deliverable is delivered OUT through a configured connector
(notion is the v1 pattern-setter). The delivery TARGET binding lives on the
``connector_accounts`` row as a JSON ``delivery_config`` dict carrying the
STABLE routing / system fields the connector's ``@p.outbound`` needs (e.g.
notion's ``parent_page_id``). Empty ``{}`` = inbound-only (no outbound
delivery). Existing rows backfill to ``{}``.

Revision ID: connector_delivery_config
Revises: decision_resolve
Create Date: 2026-06-01
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "connector_delivery_config"
down_revision: Union[str, Sequence[str], None] = "decision_resolve"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add NOT NULL with a server_default so existing rows backfill to '{}',
    # then drop the server default (the ORM owns the default going forward).
    op.add_column(
        "connector_accounts",
        sa.Column(
            "delivery_config",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )
    op.alter_column("connector_accounts", "delivery_config", server_default=None)


def downgrade() -> None:
    op.drop_column("connector_accounts", "delivery_config")
