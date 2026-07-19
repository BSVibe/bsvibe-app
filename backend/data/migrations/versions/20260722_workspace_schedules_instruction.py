"""workspace_schedules — natural-language ``instruction`` schedules (S1).

The ``workspace_schedules`` table shipped with the M1 runner but had no
producer and no free-text instruction column — the runner polled it in prod
while nothing wrote rows, a dead channel. S1 makes it authorable:

* ``kind`` (VARCHAR(32), NOT NULL, default ``'instruction'``) — what the
  schedule fires. S1 supports only ``instruction``.
* ``payload`` (JSON, NOT NULL, default ``{}``) — the instruction envelope
  ``{"text": "<what to do>"}``; the run framer reads ``text``.
* ``title`` (VARCHAR(500), NULL) — optional list-display label.
* ``plugin_name`` — dropped NOT NULL (NULL for the ``instruction`` kind).
* the ``(workspace_id, plugin_name, cron_expr)`` unique constraint — dropped
  in favor of the surrogate ``id`` (two NL rows may share a cron expr, and
  ``plugin_name`` is NULL).

Additive + reversible: the downgrade re-imposes NOT NULL on ``plugin_name``
(backfilling NULLs to '') and restores the old unique constraint, then drops
the new columns.

Revision ID: workspace_schedules_instruction
Revises: notification_outbox
Create Date: 2026-07-22
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "workspace_schedules_instruction"
down_revision: Union[str, Sequence[str], None] = "notification_outbox"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_UNIQUE = "uq_workspace_schedules_ws_plugin_cron"


def upgrade() -> None:
    op.add_column(
        "workspace_schedules",
        sa.Column(
            "kind",
            sa.String(length=32),
            nullable=False,
            server_default="instruction",
        ),
    )
    op.add_column(
        "workspace_schedules",
        sa.Column(
            "payload",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )
    op.add_column(
        "workspace_schedules",
        sa.Column("title", sa.String(length=500), nullable=True),
    )
    op.drop_constraint(_UNIQUE, "workspace_schedules", type_="unique")
    op.alter_column(
        "workspace_schedules", "plugin_name", existing_type=sa.String(length=255), nullable=True
    )


def downgrade() -> None:
    # Restore NOT NULL on plugin_name — backfill any NULLs (NL rows) to ''
    # first so the constraint can be re-imposed on existing data.
    op.execute(sa.text("UPDATE workspace_schedules SET plugin_name = '' WHERE plugin_name IS NULL"))
    op.alter_column(
        "workspace_schedules", "plugin_name", existing_type=sa.String(length=255), nullable=False
    )
    op.create_unique_constraint(
        _UNIQUE,
        "workspace_schedules",
        ["workspace_id", "plugin_name", "cron_expr"],
    )
    op.drop_column("workspace_schedules", "title")
    op.drop_column("workspace_schedules", "payload")
    op.drop_column("workspace_schedules", "kind")
