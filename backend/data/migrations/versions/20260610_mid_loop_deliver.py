"""mid_loop_deliver — B12a: ``run_id`` on delivery events + Safe Mode queue items.

Workflow §1 (mid-loop Deliver events) + §1.2 (Safe Mode as per-Run
transactional container). Two additive, nullable columns + matching indices
so existing rows survive the migration unchanged:

* ``delivery_events.run_id UUID NULL`` + index ``ix_delivery_events_run``
* ``safe_mode_queue_items.run_id UUID NULL`` + index
  ``ix_safe_mode_queue_ws_run`` on ``(workspace_id, run_id)``

The columns are NULLABLE so pre-B12a rows continue to work; new rows
threaded by the DeliveryWorker always set the value. No FK to
``execution_runs`` — same reasoning as ``delivery_events.deliverable_id``
(cross-Base boundary; integrity is enforced at the application layer +
nullable column is the safety valve for the rare orphan case).

Revision ID: mid_loop_deliver
Revises: resource_bindings
Create Date: 2026-06-10
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "mid_loop_deliver"
down_revision: Union[str, Sequence[str], None] = "resource_bindings"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "delivery_events",
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_index("ix_delivery_events_run", "delivery_events", ["run_id"])

    op.add_column(
        "safe_mode_queue_items",
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_index(
        "ix_safe_mode_queue_ws_run",
        "safe_mode_queue_items",
        ["workspace_id", "run_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_safe_mode_queue_ws_run", table_name="safe_mode_queue_items")
    op.drop_column("safe_mode_queue_items", "run_id")
    op.drop_index("ix_delivery_events_run", table_name="delivery_events")
    op.drop_column("delivery_events", "run_id")
