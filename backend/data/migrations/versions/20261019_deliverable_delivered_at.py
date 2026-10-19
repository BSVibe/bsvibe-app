"""deliverables.delivered_at — "this output went out" as a fact, not an event

Measured 2026-10-19: the scheduled weekly report's deliverable went out to Telegram while its
run was still RUNNING. Delivery success is where a connector-delivered run gets shipped, but
only a REVIEW_READY run — and when the run got there four minutes later nothing asked again,
so it sat at REVIEW_READY although its output had been delivered. With the delivery recorded,
the REVIEW_READY transition (and a sweep) can ask it afterwards.

Backfill: a deliverable that carries compensation handles was delivered — handles are written
only for successful outbound actions. Its ``created_at`` stands in for the unrecorded send time.

Revision ID: deliverable_delivered_at
Revises: worker_claude_auth_failed
Create Date: 2026-10-19
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "deliverable_delivered_at"
down_revision: Union[str, Sequence[str], None] = "worker_claude_auth_failed"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "deliverables",
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        sa.text(
            "UPDATE deliverables SET delivered_at = created_at "
            "WHERE compensation_handles IS NOT NULL "
            "AND json_typeof(compensation_handles::json) = 'array' "
            "AND json_array_length(compensation_handles::json) > 0"
        )
    )


def downgrade() -> None:
    op.drop_column("deliverables", "delivered_at")
