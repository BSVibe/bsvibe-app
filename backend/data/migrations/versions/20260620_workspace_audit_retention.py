"""workspace_audit_retention — per-workspace audit_outbox retention knob.

Lift Q1. Adds ``workspaces.audit_retention_days`` (INTEGER NULL). The
column is the founder-set knob a workspace can use to opt into
N-day rotation of its ``audit_outbox`` rows; ``NULL`` (the column
default) is the architectural default — **forever retention**, the
roadmap §6 결정 로그 Q1 founder-locked choice.

The daily sweep that actually deletes rows past
``occurred_at < now - retention_days * 1d`` lives at
:mod:`plugin.audit.retention_sweep` and is wired into the worker
runtime as a third :class:`ScheduleWorker` instance against the M1
:class:`ScheduleRunnerProtocol` seam. This migration only owns the
column; the sweep skips every ``audit_retention_days IS NULL``
workspace (no deletion).

Reversible: ``downgrade`` drops the column. No backfill — every
existing row stays ``NULL`` (= forever), which is the architecturally
correct default.

Revision ID: workspace_audit_retention
Revises: workspace_schedules
Create Date: 2026-06-02
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "workspace_audit_retention"
down_revision: Union[str, Sequence[str], None] = "workspace_schedules"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "workspaces",
        sa.Column(
            "audit_retention_days",
            sa.Integer(),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("workspaces", "audit_retention_days")
