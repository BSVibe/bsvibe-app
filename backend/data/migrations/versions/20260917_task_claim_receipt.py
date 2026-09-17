"""executor_tasks.claimed_at + executor_workers.protocol_version — receipt for a dispatch

#965. A dispatched task had no recorded moment of *receipt*. The delivery hop
acks before it answers — ``POST /api/v1/workers/poll`` hands ``consume_once`` a
handler that only appends to a list, so the XACK completes before the HTTP
response exists — and the poll never passes ``min_idle_ms``, so the worker's
stream is never XAUTOCLAIMed either. A lost response therefore left nothing
pending and nothing recorded: the run sat in ``dispatched`` until its awaiter's
timeout, and no query could separate "the worker never got it" from "the worker
is thirty minutes into a long turn".

``claimed_at`` is that missing fact, stamped by an atomic conditional UPDATE. A
claimed row is no longer a redelivery candidate — so duplicate protection comes
from the WHERE, not from a lease timer, and a turn that legitimately runs for an
hour is structurally safe.

``protocol_version`` gates redelivery. It defaults to **1**, which is what every
worker running today reports simply by not sending the header, and version 1 is
never redelivered to: such a build does not claim, and ``run_once`` there spawns
every execute message it receives (its ``_RUNNING_TASKS`` map is read only on
the cancel path), so a duplicate would run twice. The default therefore
preserves exactly today's behaviour until a worker announces otherwise —
fail-closed, and no backfill.

⚠ The worker table is ``executor_workers``. A table literally named ``workers``
existed until ``drop_dead_worker_tables`` (2026-08-21) removed it — it had never
held a row. Writing this migration against that name passes every SQLite unit
test, because ``Base.metadata.create_all`` builds whatever ``__tablename__``
says and the ORM never reads the migration; it fails only on a real Postgres.

Both columns are nullable/defaulted additions with no index: neither is ever the
sole predicate of a query. Redelivery is decided per awaited task by primary key.

Revision ID: task_claim_receipt
Revises: workspace_token_budget
Create Date: 2026-09-17
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

# ⚠ Revision ids are capped at 32 chars by ``alembic_version.version_num`` —
# a longer one fails at the UPDATE, not at the DDL. This is 18.
revision: str = "task_claim_receipt"
down_revision: Union[str, Sequence[str], None] = "workspace_token_budget"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "executor_tasks",
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
    )
    # NOT NULL with a DDL default so rows predating this read as version 1
    # without a backfill pass — and so a worker that never sends the header
    # keeps reading as 1 forever, which is the fail-closed answer.
    op.add_column(
        "executor_workers",
        sa.Column(
            "protocol_version",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
    )


def downgrade() -> None:
    op.drop_column("executor_workers", "protocol_version")
    op.drop_column("executor_tasks", "claimed_at")
