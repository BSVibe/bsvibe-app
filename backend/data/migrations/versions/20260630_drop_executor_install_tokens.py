"""drop executor_install_tokens — Lift E5 removes the legacy install-token system.

Worker registration now uses the host OAuth bearer (Lift E4 —
``bsvibe-worker register --name X`` sends ``Authorization: Bearer``). The
``executor_install_tokens`` table and the ``X-Install-Token`` register path
are gone; this drops the now-unreferenced table.

Idempotent: ``DROP TABLE IF EXISTS`` so re-running on a DB that already
dropped the table (or one provisioned post-E5) is a no-op.

Reversible: ``downgrade`` re-creates the original schema (mirrors the
original ``executor_workers`` migration's table definition).

Revision ID: drop_executor_install_tokens
Revises: run_routing_caller_id
Create Date: 2026-06-06
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "drop_executor_install_tokens"
down_revision: Union[str, Sequence[str], None] = "run_routing_caller_id"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Drop indexes first (no-op on Postgres if the table is already gone —
    # ``DROP INDEX IF EXISTS`` would be the cleanest, but op.drop_index lacks
    # an idempotent flag, so wrap in a raw guarded SQL where needed).
    bind = op.get_bind()
    dialect = bind.dialect.name
    if dialect == "postgresql":
        op.execute("DROP INDEX IF EXISTS ix_executor_install_tokens_token_hash")
        op.execute("DROP INDEX IF EXISTS ix_executor_install_tokens_workspace_id")
        op.execute("DROP TABLE IF EXISTS executor_install_tokens")
    else:
        # SQLite test tier — straight drop; SQLAlchemy's batch ops aren't
        # needed because we're dropping the whole table.
        op.execute("DROP TABLE IF EXISTS executor_install_tokens")


def downgrade() -> None:
    op.create_table(
        "executor_install_tokens",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("token_hash", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", name="uq_executor_install_tokens_workspace"),
    )
    op.create_index(
        "ix_executor_install_tokens_workspace_id",
        "executor_install_tokens",
        ["workspace_id"],
    )
    op.create_index(
        "ix_executor_install_tokens_token_hash",
        "executor_install_tokens",
        ["token_hash"],
    )
