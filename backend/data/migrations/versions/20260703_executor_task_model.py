"""executor_task_model — per-task model id for capability-aware routing.

Lift E21. Adds one nullable string column to ``executor_tasks`` so the
backend can forward the LLM model the founder selected for this caller
(via :class:`ModelAccount.litellm_model`) onto the worker's stream entry,
and the worker can forward it into the executor's HTTP body.

Without this, every ``opencode``/``codex``/``claude_code`` chat call uses
the CLI's hard-coded default model (e.g. ``opencode-go``'s ``plan`` agent
defaults to ``claude-haiku-4-5``, which is NOT in the opencode-go plan and
401s with "Insufficient balance"). Founder routes ``ingest`` to
``opencode-go/qwen3.6-plus`` and ``codegen`` to ``opencode-go/kimi-k2.6``
by giving each ModelAccount a different ``litellm_model`` value; that
value flows here.

* ``model`` (``VARCHAR(255)``, nullable, no default) — the underlying LLM
  model id. ``NULL`` on every legacy row and a back-compat signal to the
  worker ("use the CLI default"). Worker code already calls
  ``task.get("model") or None`` so a missing column is harmless.

Safe to run online — column is nullable, no backfill needed. Down
migration drops it cleanly.

Revision ID: executor_task_model
Revises: worker_last_in_flight
Create Date: 2026-06-11
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "executor_task_model"
down_revision: Union[str, Sequence[str], None] = "worker_last_in_flight"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "executor_tasks",
        sa.Column("model", sa.String(length=255), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("executor_tasks", "model")
