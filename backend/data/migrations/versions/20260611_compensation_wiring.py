"""compensation_wiring — B12b: capture compensation_handle + retracted_at on Deliverable.

Workflow §1.2 + §3.1 + §9. A plugin's ``@p.outbound`` returns a
``compensation_handle`` (plugin-private revert token) so the paired
``@p.compensate`` handler can later roll the artifact back. Pre-B12b the
handle was logged + thrown away and there was no path back to it — direct-
mode deliveries could never be retracted.

Two additive, nullable columns on ``deliverables`` so existing rows survive
the migration unchanged:

* ``deliverables.compensation_handles JSON NULL`` — list of
  ``{"plugin", "artifact_type", "handle"}`` entries, populated by
  :func:`backend.workflow.infrastructure.workers.delivery_worker.dispatch_delivery` after a successful
  outbound action. ``NULL`` → nothing to revert (pre-B12b or plugin opted out).
* ``deliverables.retracted_at TIMESTAMPTZ NULL`` — set by
  ``POST /api/v1/deliverables/{deliverable_id}/retract`` after the plugin
  compensate dispatch succeeds. NOT NULL → row is retracted; idempotent re-call
  returns 200 no-op.

No new indices: lookups go by primary key (``deliverable_id``) from the retract
endpoint; ``compensation_handles`` is read-as-bag, not filtered against.

Revision ID: compensation_wiring
Revises: mid_loop_deliver
Create Date: 2026-06-11
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "compensation_wiring"
down_revision: Union[str, Sequence[str], None] = "mid_loop_deliver"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "deliverables",
        sa.Column("compensation_handles", sa.JSON(), nullable=True),
    )
    op.add_column(
        "deliverables",
        sa.Column("retracted_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("deliverables", "retracted_at")
    op.drop_column("deliverables", "compensation_handles")
