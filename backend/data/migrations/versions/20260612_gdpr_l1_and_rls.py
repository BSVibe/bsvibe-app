"""gdpr_l1_and_rls — GDPR L1 column + Postgres RLS defense layer 3.

Two compliance gaps from the B17 audit:

1. **GDPR L1**: ``Workspace.legal_basis`` was missing. Adds a TEXT column with
   ``server_default='contract'`` so existing rows get the production-correct
   value with no app-tier migration. Validation lives in the ORM layer via
   :data:`backend.identity.workspaces_db.validate_legal_basis` (``Literal["contract",
   "consent"]``).

2. **Postgres RLS defense layer 3**: enables ROW LEVEL SECURITY on a focused
   set of workspace-scoped root tables and installs a policy keyed off the
   per-session GUC ``app.current_workspace_id``. The GUC is set by
   :func:`backend.data.rls.set_workspace_guc` after the request-context
   workspace resolution. A compromised app server that bypasses the ORM
   filter is still blocked at the database itself.

Postgres subtleties locked in by this migration
-----------------------------------------------
* ``FORCE ROW LEVEL SECURITY`` is set in addition to ``ENABLE`` — without
  FORCE, table-owner sessions (typical for a dev / migration role) bypass
  the policy entirely. We want everyone to honour the policy.
* The policy USING clause is ``current_setting(GUC, true)::uuid IS NULL
  OR workspace_id = current_setting(GUC, true)::uuid`` — when the GUC is
  unset (boot scripts, alembic, maintenance) the table is fully visible;
  when set, only the matching workspace_id is. ``true`` to
  ``current_setting`` lets it return NULL rather than ERROR for an unset
  GUC.

SQLite has no row-security; the RLS block is dialect-gated and skipped
on SQLite so the unit-test tier still creates the column.

Revision ID: gdpr_l1_and_rls
Revises: compensation_wiring
Create Date: 2026-06-12
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "gdpr_l1_and_rls"
down_revision: Union[str, Sequence[str], None] = "compensation_wiring"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Tables that get RLS turned on. Each carries a ``workspace_id`` column and
# is a *root* user-data surface — we deliberately don't extend RLS to every
# join table because the parent-FK CASCADE already constrains the children,
# and the ORM auto-filter (layer 2) covers them. This focused set is what
# the audit asked for.
_RLS_TABLES: tuple[str, ...] = (
    "workspaces",
    "products",
    "execution_runs",
    "deliverables",
    "execution_decisions",
    "requests",
)

# The workspaces table is keyed on ``id``, not ``workspace_id``. Every other
# table in _RLS_TABLES uses the column name ``workspace_id``.
_WS_COLUMN: dict[str, str] = {"workspaces": "id"}

_POLICY_NAME = "rls_workspace_isolation"
_GUC = "app.current_workspace_id"


def upgrade() -> None:
    # --- GDPR L1 column ------------------------------------------------------
    op.add_column(
        "workspaces",
        sa.Column(
            "legal_basis",
            sa.String(length=32),
            nullable=False,
            server_default="contract",
        ),
    )

    # --- Postgres RLS — skipped on SQLite (no row-security feature) ---------
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    for table in _RLS_TABLES:
        col = _WS_COLUMN.get(table, "workspace_id")
        # ENABLE + FORCE so even the table owner honours the policy.
        op.execute(sa.text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
        op.execute(sa.text(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"))
        op.execute(
            sa.text(
                f"CREATE POLICY {_POLICY_NAME} ON {table} "
                "USING ("
                f"current_setting('{_GUC}', true) IS NULL OR "
                f"current_setting('{_GUC}', true) = '' OR "
                f"{col}::text = current_setting('{_GUC}', true)"
                ") "
                "WITH CHECK ("
                f"current_setting('{_GUC}', true) IS NULL OR "
                f"current_setting('{_GUC}', true) = '' OR "
                f"{col}::text = current_setting('{_GUC}', true)"
                ")"
            )
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        for table in _RLS_TABLES:
            op.execute(sa.text(f"DROP POLICY IF EXISTS {_POLICY_NAME} ON {table}"))
            op.execute(sa.text(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY"))
            op.execute(sa.text(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY"))

    op.drop_column("workspaces", "legal_basis")
