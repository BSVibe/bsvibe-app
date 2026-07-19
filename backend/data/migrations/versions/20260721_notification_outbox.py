"""notification_outbox — durable founder-notification outbox (Notifier N2).

The transactional-outbox table behind the ``notification_outbox``
:class:`~backend.channels.Channel`. ``create_decision`` stages one ``needs_you``
row here inside the Decision's transaction; the ``NotifyWorker`` drains pending
rows under ``FOR UPDATE SKIP LOCKED`` and delivers the founder's enabled push
channels.

* ``dedupe_key`` (``VARCHAR(128)``, UNIQUE) — one notification per moment
  (``needs_you:<decision_id>``); a re-emit is a DB-level no-op.
* ``status`` — ``notification_status_enum`` (pending / sent / failed); a
  partial index on ``(status, created_at)`` keeps the pending-claim scan cheap.
* ``payload`` (JSON) — the channel-agnostic ``{title, body, link, run_id?,
  decision_id?}`` the per-connector notify builders shape.

Reversible: the downgrade drops the table then the enum type.

Revision ID: notification_outbox
Revises: workspace_timezone
Create Date: 2026-07-21
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "notification_outbox"
down_revision: Union[str, Sequence[str], None] = "workspace_timezone"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_STATUS_VALUES = ("pending", "sent", "failed")
_STATUS = postgresql.ENUM(*_STATUS_VALUES, name="notification_status_enum", create_type=False)


def upgrade() -> None:
    bind = op.get_bind()
    sa.Enum(*_STATUS_VALUES, name="notification_status_enum").create(bind, checkfirst=True)

    op.create_table(
        "notification_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event", sa.String(length=32), nullable=False),
        sa.Column("dedupe_key", sa.String(length=128), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", _STATUS, nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dedupe_key", name="uq_notification_events_dedupe_key"),
    )
    op.create_index(
        "ix_notification_events_status_created",
        "notification_events",
        ["status", "created_at"],
    )
    op.create_index(
        "ix_notification_events_workspace",
        "notification_events",
        ["workspace_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_notification_events_workspace", table_name="notification_events")
    op.drop_index("ix_notification_events_status_created", table_name="notification_events")
    op.drop_table("notification_events")
    bind = op.get_bind()
    sa.Enum(name="notification_status_enum").drop(bind, checkfirst=True)
