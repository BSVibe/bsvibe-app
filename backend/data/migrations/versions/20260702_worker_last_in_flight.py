"""worker_last_in_flight — per-worker in-flight task count for capacity-aware dispatch.

Lift E16. Adds one nullable integer column to ``executor_workers`` so the
worker's heartbeat can stamp its current in-flight task count and the
backend's :func:`find_available_worker` can exclude workers already at
their parallel-task cap. Without this, the backend kept dispatching onto a
worker stream the worker had stopped polling (its poll loop skips polling
while ``len(in_flight) >= max_parallel_tasks``), so the per-task 600 s
``await_completion`` timer ran against tasks the worker had not yet read
— marking chunks ``failed`` before they were even started.

* ``last_in_flight`` (``INTEGER``, nullable, default ``0``) — the worker's
  ``len(in_flight)`` at its last heartbeat. ``NULL`` on every legacy row
  until a freshly-upgraded E16 worker sends its first heartbeat (the new
  field defaults to ``0`` in the schema for new rows). The backend treats
  ``NULL`` as "no signal — pre-E16 worker, fall through to old behaviour"
  so the rollout is back-compat: a stale-shape worker is never
  capacity-excluded just because it never reported a count.

Safe to run online — column is nullable with a default 0, no backfill
needed. Down migration drops it cleanly.

Revision ID: worker_last_in_flight
Revises: connector_oauth_unclaimed
Create Date: 2026-06-08
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "worker_last_in_flight"
down_revision: Union[str, Sequence[str], None] = "connector_oauth_unclaimed"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "executor_workers",
        sa.Column("last_in_flight", sa.Integer(), nullable=True, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("executor_workers", "last_in_flight")
