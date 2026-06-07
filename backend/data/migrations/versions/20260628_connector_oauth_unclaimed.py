"""connector_oauth_unclaimed — installs awaiting a workspace claim (Lift 8).

Sentry's install→grant redirect carries no state, so the callback exchanges the
grant code and parks the token here unbound; the founder claims it for a
workspace later (design §11). Encrypted at rest; installation_ref is the Sentry
installationId (needed for refresh after claim).

Revision ID: connector_oauth_unclaimed
Revises: product_bootstrap_progress
Create Date: 2026-06-08
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "connector_oauth_unclaimed"
down_revision: Union[str, Sequence[str], None] = "product_bootstrap_progress"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "connector_oauth_unclaimed",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("installation_ref", sa.String(length=255), nullable=False),
        sa.Column("account_label", sa.String(length=255), nullable=True),
        sa.Column("access_token_ciphertext", sa.String(length=2048), nullable=False),
        sa.Column("refresh_token_ciphertext", sa.String(length=2048), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )


def downgrade() -> None:
    op.drop_table("connector_oauth_unclaimed")
