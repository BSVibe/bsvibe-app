"""safe_mode_lifecycle — D3: extend the Safe Mode queue lifecycle states.

Workflow §10.5 / Synthesis §11. The ``delivery_safe_mode_status_enum`` gains
three post-decision lifecycle values so a queue item has a full
``pending → approved → delivered → archived → deleted`` path (plus the existing
``denied`` / ``expired`` terminals):

* ``delivered`` — an approved item's outbound dispatch succeeded.
* ``archived``  — a settled item parked out of the active queue.
* ``deleted``   — the soft-tombstone the retention sweep flips post-archive.

Extending a Postgres ENUM via ``ALTER TYPE ADD VALUE`` is non-reversible and
cannot run inside a transaction on older PG — so we use the rename/recreate
pattern (alembic-postgres-enum-migration): rename the old type aside, create a
fresh type carrying the full value set, retype the column with a ``USING`` cast,
then drop the renamed old type. The downgrade is the symmetric inverse (no row
may hold a D3-only value when downgrading; this matches every other reversible
enum migration in the chain).

The TEXT-backed SQLite test tier does not enforce enum membership, so this DDL
is PG-only and a no-op on SQLite (guarded on the dialect).

Revision ID: safe_mode_lifecycle
Revises: note_embeddings
Create Date: 2026-06-18
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

from alembic import op

revision: str = "safe_mode_lifecycle"
down_revision: Union[str, Sequence[str], None] = "note_embeddings"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ENUM = "delivery_safe_mode_status_enum"
_OLD_VALUES = ("pending", "approved", "denied", "expired", "extended")
_NEW_VALUES = (*_OLD_VALUES, "delivered", "archived", "deleted")


def _recreate_enum(values: tuple[str, ...]) -> None:
    """Retype ``safe_mode_queue_items.status`` onto a freshly-built enum.

    Renames the live type aside, creates a new type holding ``values``, retypes
    the column via a ``USING`` cast (cast through text so the old labels map by
    name), then drops the renamed type. Symmetric for upgrade + downgrade.
    """
    labels = ", ".join(f"'{v}'" for v in values)
    op.execute(f"ALTER TYPE {_ENUM} RENAME TO {_ENUM}_old")
    op.execute(f"CREATE TYPE {_ENUM} AS ENUM ({labels})")
    op.execute(
        f"ALTER TABLE safe_mode_queue_items "
        f"ALTER COLUMN status TYPE {_ENUM} "
        f"USING status::text::{_ENUM}"
    )
    op.execute(f"DROP TYPE {_ENUM}_old")


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    _recreate_enum(_NEW_VALUES)


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    # Any row still holding a D3-only value would fail the USING cast — collapse
    # the post-decision states back onto their pre-D3 nearest terminal so the
    # downgrade is safe on a populated DB.
    op.execute(
        "UPDATE safe_mode_queue_items SET status = 'approved' "
        "WHERE status IN ('delivered', 'archived', 'deleted')"
    )
    _recreate_enum(_OLD_VALUES)
