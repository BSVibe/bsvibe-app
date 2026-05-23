"""decision_resolve — resolution lifecycle fields on execution_decisions.

Workflow §5 #4 / §12.5 #8: a ``needs_decision`` checkpoint pauses the run
(run stays RUNNING). To let the founder resolve it and resume the run, the
``execution_decisions`` row gains a resolution lifecycle: ``status`` (pending
→ resolved), the ``resolution`` answer text, ``resolved_at`` and
``resolved_by``. Existing rows backfill to ``pending``.

Revision ID: decision_resolve
Revises: connector_accounts
Create Date: 2026-05-31
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "decision_resolve"
down_revision: Union[str, Sequence[str], None] = "connector_accounts"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_DECISION_STATUS_VALUES = ("pending", "resolved")
_DECISION_STATUS = postgresql.ENUM(
    *_DECISION_STATUS_VALUES, name="decision_status_enum", create_type=False
)


def upgrade() -> None:
    bind = op.get_bind()
    sa.Enum(*_DECISION_STATUS_VALUES, name="decision_status_enum").create(bind, checkfirst=True)

    # Add nullable first + server_default so existing rows backfill to pending,
    # then drop the server default (the ORM owns the default going forward).
    op.add_column(
        "execution_decisions",
        sa.Column(
            "status",
            _DECISION_STATUS,
            nullable=False,
            server_default="pending",
        ),
    )
    op.alter_column("execution_decisions", "status", server_default=None)
    op.add_column("execution_decisions", sa.Column("resolution", sa.Text(), nullable=True))
    op.add_column(
        "execution_decisions",
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "execution_decisions",
        sa.Column("resolved_by", postgresql.UUID(as_uuid=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("execution_decisions", "resolved_by")
    op.drop_column("execution_decisions", "resolved_at")
    op.drop_column("execution_decisions", "resolution")
    op.drop_column("execution_decisions", "status")
    bind = op.get_bind()
    sa.Enum(name="decision_status_enum").drop(bind, checkfirst=True)
