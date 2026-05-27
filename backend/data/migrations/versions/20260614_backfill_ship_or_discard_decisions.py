"""backfill_ship_or_discard_decisions — L-P2: surface existing REVIEW_READY runs.

Pre-L-P2, a run that verified left the founder UI silent: no Decision was
ever minted on the REVIEW_READY transition, so the run sat invisible in
Decisions and on the product detail page never lit up. e2e-hello reality
audit (2026-05-27) found this in production with 2 verified runs the
founder never saw.

This migration backfills one ``ship_or_discard`` Decision per existing
REVIEW_READY run that has none — so the existing in-flight verified work
becomes actionable as soon as the lift lands.

Postgres only. The SQLite test tier rebuilds from ``create_all`` and
never carries pre-lift rows that need backfill.

Revision ID: backfill_ship_or_discard_decisions
Revises: product_id_not_null
Create Date: 2026-05-27
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "backfill_ship_or_discard_decisions"
down_revision: Union[str, Sequence[str], None] = "product_id_not_null"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_BACKFILL_SQL = """
INSERT INTO execution_decisions (
    id, run_id, workspace_id, decision, payload, status, created_at
)
SELECT
    gen_random_uuid(),
    r.id,
    r.workspace_id,
    'ship_or_discard',
    '{"reason": "review_ready"}'::json,
    'pending',
    NOW()
FROM execution_runs r
WHERE r.status = 'review_ready'
  AND NOT EXISTS (
      SELECT 1
      FROM execution_decisions d
      WHERE d.run_id = r.id
        AND d.decision = 'ship_or_discard'
        AND d.status = 'pending'
  )
"""


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(sa.text(_BACKFILL_SQL))


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            sa.text(
                "DELETE FROM execution_decisions "
                "WHERE decision = 'ship_or_discard' AND status = 'pending'"
            )
        )
