"""notification_channel_keys — align matrix channel keys with connector names.

Notifier N1a derives a workspace's notification channels from its connector
bindings: the channel id IS the ``connector_accounts.connector`` value. The
pre-N1a seed used a hardcoded ``("in_app", "email", "slack")`` grid whose
``"email"`` key does NOT match the email connector's name (``email-sender`` —
the plugin ``name=``). ``"slack"`` already matches, and ``"in_app"`` is the
non-connector inbox, so ``"email"`` is the only mismatched key.

This migration renames the JSON matrix key ``"email" -> "email-sender"`` in every
``notification_prefs.matrix`` cell so a workspace that had email enabled keeps
that preference under the key the derived channel model now uses. Idempotent (a
second run finds no ``"email"`` key), dialect-agnostic (iterates rows and lets
the SQLAlchemy JSON type serialize per dialect), and reversible.

Revision ID: notification_channel_keys
Revises: runtime_role
Create Date: 2026-07-19
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Union

import sqlalchemy as sa
from alembic import op

revision: str = "notification_channel_keys"
down_revision: Union[str, Sequence[str], None] = "runtime_role"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OLD_KEY = "email"
_NEW_KEY = "email-sender"

# A minimal Core table so the JSON column round-trips through the dialect's
# JSON (de)serializer on both SQLite and Postgres — no manual json.loads/dumps
# or dialect-specific casts.
_prefs = sa.table(
    "notification_prefs",
    sa.column("id", sa.Uuid),
    sa.column("matrix", sa.JSON),
)


def _rename_key(matrix: dict[str, Any], old: str, new: str) -> bool:
    """Rename ``old -> new`` in every event's channel dict. Returns True if any
    cell changed. ``setdefault`` never clobbers an existing ``new`` key, so the
    op is safe when both keys coexist and is idempotent on re-run."""
    changed = False
    for channels in matrix.values():
        if isinstance(channels, dict) and old in channels:
            channels.setdefault(new, channels[old])
            del channels[old]
            changed = True
    return changed


def _remap(old: str, new: str) -> None:
    bind = op.get_bind()
    rows = bind.execute(sa.select(_prefs.c.id, _prefs.c.matrix)).all()
    for row_id, matrix in rows:
        if not isinstance(matrix, dict):
            continue
        if _rename_key(matrix, old, new):
            bind.execute(_prefs.update().where(_prefs.c.id == row_id).values(matrix=matrix))


def upgrade() -> None:
    _remap(_OLD_KEY, _NEW_KEY)


def downgrade() -> None:
    _remap(_NEW_KEY, _OLD_KEY)
