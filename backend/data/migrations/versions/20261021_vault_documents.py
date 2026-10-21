"""vault_documents — the vault and skills as rows, Markdown unchanged (#929, B — stage 2).

The knowledge vault (``var/vault``) and skills (``var/skills``) were files on one machine's
disk. With more than one machine they have to be one shared set, so they move into this table:
``content`` is exactly the file, ``path`` exactly its tree-relative path, ``tree`` says which of
the two trees. Founder decision 2026-10-21 (Postgres rows, not R2 objects or a shared mount) —
docs/design/stateless-workers.md §5.

Layer-2 table (ORM auto-filter by ``workspace_id``), like ``note_embeddings``.

Revision ID: vault_documents
Revises: run_home_machine
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "vault_documents"
down_revision = "run_home_machine"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "vault_documents",
        sa.Column("workspace_id", sa.Uuid(), primary_key=True),
        sa.Column("tree", sa.String(16), primary_key=True),
        sa.Column("path", sa.Text(), primary_key=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("vault_documents")
