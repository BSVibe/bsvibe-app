"""G3 — note_embeddings (pgvector) for knowledge-note semantic search.

Mirrors the gateway's intent_examples embedding policy: a pgvector ``vector``
column with the ``<=>`` cosine-distance operator, scoped per workspace. The
``vector`` extension is created by the routing migration already; ``IF NOT
EXISTS`` keeps this safe on a fresh DB too.

Revision ID: note_embeddings
Revises: run_routing_rules
Create Date: 2026-05-29
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision: str = "note_embeddings"
down_revision: Union[str, Sequence[str], None] = "run_routing_rules"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table(
        "note_embeddings",
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("note_path", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(None), nullable=True),
        sa.Column("embedding_model", sa.String(length=120), nullable=True),
        sa.Column("dimension", sa.Integer(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "note_path"),
    )
    op.create_index(
        "ix_note_embeddings_workspace", "note_embeddings", ["workspace_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_note_embeddings_workspace", table_name="note_embeddings")
    op.drop_table("note_embeddings")
