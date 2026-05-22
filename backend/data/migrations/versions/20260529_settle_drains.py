"""settle_drains — idempotency marker for the SettleWorker BSage write subscriber.

Workflow §4 (``worker-settle``) + §5 (the trust ratchet's learning half).
One row per ``settle`` activity already absorbed into a workspace's BSage
vault, keyed by ``activity_id`` so a re-drain is a no-op. Not a deletable
queue — the source ``execution_run_activities`` rows stay as append-only
telemetry, so settlement is marked out-of-band here instead.

Revision ID: settle_drains
Revises: phase1_auth_identity
Create Date: 2026-05-29
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "settle_drains"
down_revision: Union[str, Sequence[str], None] = "phase1_auth_identity"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "settle_drains",
        sa.Column("activity_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("node_ref", sa.Text(), nullable=True),
        sa.Column("drained_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_settle_drains_workspace_id", "settle_drains", ["workspace_id"])


def downgrade() -> None:
    op.drop_index("ix_settle_drains_workspace_id", table_name="settle_drains")
    op.drop_table("settle_drains")
