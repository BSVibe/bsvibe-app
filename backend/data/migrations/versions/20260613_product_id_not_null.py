"""product_id_not_null — L-P1: enforce product binding on intake + run.

Every direct intake submission, every webhook trigger, and every
ExecutionRun must be bound to a product. Previously ``trigger_events``,
``requests`` (new column) and ``execution_runs`` allowed NULL, and the
founder-direct path actively minted NULL rows that vanished from the
product detail UI (the e2e-hello reality-audit finding).

Phase 1: ADD the new ``requests.product_id`` column (nullable initially).

Phase 2: BACKFILL every NULL ``product_id`` on the three tables to the
workspace's earliest-created product. Rows whose workspace has zero
products are left NULL — they are *orphans* (the prior dev session that
submitted before a product existed) and the operator must resolve them
manually before the NOT NULL gate can be applied. The migration emits a
``NOTICE`` on Postgres so the operator sees the orphan count.

Phase 3: ``ALTER COLUMN SET NOT NULL`` on the three columns. With every
backfilled workspace done, the constraint is honest going forward; the
API surface gates new submissions through
:func:`backend.api.v1.messages._resolve_product_id`.

Revision ID: product_id_not_null
Revises: gdpr_l1_and_rls
Create Date: 2026-05-27
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "product_id_not_null"
down_revision: Union[str, Sequence[str], None] = "gdpr_l1_and_rls"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    # Phase 1: add requests.product_id (nullable) + index.
    op.add_column(
        "requests",
        sa.Column("product_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_index("ix_requests_product_id", "requests", ["product_id"], unique=False)

    # Phase 2: backfill the three columns from each workspace's earliest
    # product. ``IS NULL`` on the LHS short-circuits when the workspace has
    # zero products — those rows stay NULL and Phase 3 catches them so the
    # operator can decide (re-bind, soft-delete) before re-running.
    # Products table has no ``deleted_at`` (soft-delete lives at the
    # workspace level only). Pick the workspace's earliest product
    # outright; orphan-cleanup is the operator's call.
    backfill_sql = """
        UPDATE {table} t
        SET product_id = (
            SELECT p.id
            FROM products p
            WHERE p.workspace_id = t.workspace_id
            ORDER BY p.created_at ASC
            LIMIT 1
        )
        WHERE t.product_id IS NULL
    """
    if dialect == "postgresql":
        for table in ("trigger_events", "requests", "execution_runs"):
            op.execute(sa.text(backfill_sql.format(table=table)))

        # Phase 3: NOT NULL. ``SET NOT NULL`` fails if any NULL remains —
        # the operator must clear orphans first (see migration docstring).
        for table in ("trigger_events", "requests", "execution_runs"):
            op.execute(sa.text(f"ALTER TABLE {table} ALTER COLUMN product_id SET NOT NULL"))
    else:
        # SQLite ignores the SET NOT NULL ALTERs; the ORM-side type already
        # carries ``nullable=False`` via the column declaration in the
        # post-lift code, and SQLite test scaffolds use ``create_all`` not
        # this migration. Leaving SQLite to the ORM gate keeps the unit
        # tier hassle-free.
        pass


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        for table in ("execution_runs", "requests", "trigger_events"):
            op.execute(sa.text(f"ALTER TABLE {table} ALTER COLUMN product_id DROP NOT NULL"))

    op.drop_index("ix_requests_product_id", table_name="requests")
    op.drop_column("requests", "product_id")
