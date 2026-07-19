"""workspace_timezone — per-workspace IANA time zone for quiet-hours.

The founder-selected IANA zone the server evaluates quiet hours against.
Notifier N2's server-side NotifyWorker suppresses notifications during a
workspace's quiet-hours window, which requires knowing that workspace's
local time — so the zone must live on the row (the PWA selector previously
wrote it to ``localStorage`` only, where the server could never read it).

* ``timezone`` (``VARCHAR(64)``, NOT NULL, server_default ``'UTC'``) — an
  IANA zone name ("Asia/Seoul" / "UTC"). Existing rows backfill to "UTC"
  via the server default (the multi-tenant global default); new rows default
  "UTC" until the founder picks otherwise. The PWA client keeps its own
  ``Asia/Seoul`` pre-load placeholder — that is a display default, not the
  stored value, so the backfill deliberately stays "UTC".

Safe to run online — NOT NULL with a server default backfills every existing
row in one statement. Down migration drops it cleanly.

Revision ID: workspace_timezone
Revises: notification_channel_keys
Create Date: 2026-07-20
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "workspace_timezone"
down_revision: Union[str, Sequence[str], None] = "notification_channel_keys"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "workspaces",
        sa.Column("timezone", sa.String(length=64), nullable=False, server_default="UTC"),
    )


def downgrade() -> None:
    op.drop_column("workspaces", "timezone")
