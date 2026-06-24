"""workspace_language — per-workspace LLM output language.

The language LLM-generated user-facing content is written in (knowledge notes,
the agent's decision questions, framing). The founder sets it via Settings →
Language; it is threaded into the generation prompts as a "respond in <lang>"
directive. NOT the FS region and NOT a routing knob.

* ``language`` (``VARCHAR(8)``, NOT NULL, server_default ``'en'``) — a short
  locale tag ("en" / "ko"). Existing rows backfill to "en" via the server
  default; new rows default "en" until the founder picks otherwise.

Safe to run online — NOT NULL with a server default backfills every existing
row in one statement. Down migration drops it cleanly.

Revision ID: workspace_language
Revises: connector_oauth_tokens_status
Create Date: 2026-06-24
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "workspace_language"
down_revision: Union[str, Sequence[str], None] = "connector_oauth_tokens_status"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "workspaces",
        sa.Column("language", sa.String(length=8), nullable=False, server_default="en"),
    )


def downgrade() -> None:
    op.drop_column("workspaces", "language")
