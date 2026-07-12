"""run_routing_source_text — NL-native routing Lift N5.

Adds ``run_routing_rules.source_text`` (VARCHAR(500) NULL): the founder's
ORIGINAL plain-language CONDITION phrase ("복잡한 작업", "마케팅 관련", "한국어
요청") that a rule was compiled from. NULL for legacy / directly-structured rules
that were never authored from natural language, so the column is nullable and no
backfill is needed.

The column is display + edit metadata: on save the phrase is compiled into the
structured ``caller_id`` / ``conditions`` (creating an intent def for a category);
editing ``source_text`` recompiles + rewrites them. The compiled structure — not
``source_text`` — is what the resolver evaluates, so an old rule with a NULL
``source_text`` keeps routing exactly as before.

Reversible: ``downgrade`` drops the column.

Revision ID: run_routing_source_text
Revises: drop_layer2_routing_rules
Create Date: 2026-07-12
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "run_routing_source_text"
down_revision: Union[str, Sequence[str], None] = "drop_layer2_routing_rules"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "run_routing_rules",
        sa.Column("source_text", sa.String(length=500), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("run_routing_rules", "source_text")
