"""ontology_corrections — durable retraction signals with undo window.

Lift M3a. Adds the ``ontology_corrections`` table — one row per founder-issued
ontology retraction / correction. The row carries the full
:class:`backend.knowledge.domain.retraction.RetractionSignal` payload as JSON
plus three lifecycle timestamps:

* ``apply_at`` — when the undo window expires; the retraction tombstone is
  committed to the vault after this time.
* ``applied_at`` — set when the apply step has run (idempotency anchor for
  the worker / lazy resolver).
* ``cancelled_at`` — set when the founder undoes inside the window.

The undo window cannot be process-local — a worker restart between intake
and the 30s mark would otherwise drop the timer. The DB row IS the timer:
``apply_at`` is the wall-clock deadline, and ``applied_at``/``cancelled_at``
are the terminal flags. A lazy-resolve check on the next read (or a tiny
sweep) commits the tombstone past the deadline.

The ``signal_json`` blob is the canonical wire shape (the audit-readable
record of what was issued + by whom + with what reason). Columns hoisted out
of the blob — ``workspace_id``, ``actor_id``, ``node_ref`` — are the ones
the resolver/idempotency query needs to filter on.

Revision ID: ontology_corrections
Revises: workspace_audit_retention
Create Date: 2026-06-02
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "ontology_corrections"
down_revision: Union[str, Sequence[str], None] = "workspace_audit_retention"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "ontology_corrections",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("node_ref", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("signal_json", sa.JSON(), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("apply_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_ontology_corrections_workspace_id",
        "ontology_corrections",
        ["workspace_id"],
    )
    op.create_index(
        "ix_ontology_corrections_node_ref",
        "ontology_corrections",
        ["workspace_id", "node_ref"],
    )
    # The lazy-resolve / sweep query: rows still in-window or just past it
    # that have not yet been finalized (apply or cancel).
    op.create_index(
        "ix_ontology_corrections_pending",
        "ontology_corrections",
        ["apply_at"],
        postgresql_where=sa.text("applied_at IS NULL AND cancelled_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_ontology_corrections_pending", table_name="ontology_corrections")
    op.drop_index("ix_ontology_corrections_node_ref", table_name="ontology_corrections")
    op.drop_index("ix_ontology_corrections_workspace_id", table_name="ontology_corrections")
    op.drop_table("ontology_corrections")
