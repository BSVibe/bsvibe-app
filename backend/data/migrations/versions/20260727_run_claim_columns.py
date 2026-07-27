"""run_claim_columns — atomic-claim coordination columns on ``execution_runs``.

Drive-session-release refactor. The AgentWorker used to hold a
``SELECT ... FOR UPDATE SKIP LOCKED`` row-lock (and the pooled DB connection
behind it) for the ENTIRE drive of a run — including the multi-minute external
executor turn. Under load that exhausted the connection pool and took the whole
backend offline. The fix converts the drive into short committed transactions
with a connection-free executor await; because the drive now commits mid-flight,
the held FOR UPDATE lock can no longer be what prevents a double-drive. These
two columns replace it:

* ``claimed_at`` (``TIMESTAMPTZ``, nullable) — stamped when a worker atomically
  claims a run (OPEN → RUNNING in a committed short txn), refreshed at each
  turn-boundary commit (a heartbeat), cleared on every drive exit.
* ``claimed_by`` (``UUID``, nullable) — the driving worker's id.

Both NULL = "not currently being driven". A stale claim (``claimed_at`` older
than the lease, no pending Decision) is reaped back to OPEN so a crashed
worker's run is re-driven. A run paused on a Decision keeps ``claimed_at`` NULL
and a pending Decision → never reaped, never re-picked (the scan is on OPEN).

Safe to run online — both columns are nullable with no backfill needed. Down
migration drops them cleanly.

Revision ID: run_claim_columns
Revises: drop_dead_execution_tables
Create Date: 2026-07-27
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "run_claim_columns"
down_revision: Union[str, Sequence[str], None] = "drop_dead_execution_tables"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "execution_runs",
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "execution_runs",
        sa.Column("claimed_by", sa.Uuid(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("execution_runs", "claimed_by")
    op.drop_column("execution_runs", "claimed_at")
