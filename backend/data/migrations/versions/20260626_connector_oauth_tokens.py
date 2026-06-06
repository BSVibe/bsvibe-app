"""connector_oauth_tokens — OAuth token + pending-state tables (Lift 0).

The connector AuthStrategy skeleton (design ~/Docs/BSVibe_Connector_OAuth_
AuthStrategy_Design_2026-06-05.md §4). Two tables:

* ``connector_oauth_tokens`` — encrypted access/refresh material, 1:1 with a
  ``connector_accounts`` binding (FK CASCADE + unique). Refresh/expiry
  nullable (non-expiring providers).
* ``connector_oauth_pending`` — single-use CSRF ``state`` + PKCE verifier
  held between /start and /callback. No FK (account may not exist yet).

Revision ID: connector_oauth_tokens
Revises: oauth_anonymous_dcr
Create Date: 2026-06-05
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "connector_oauth_tokens"
down_revision: Union[str, Sequence[str], None] = "oauth_anonymous_dcr"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "connector_oauth_tokens",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "connector_account_id",
            sa.Uuid(),
            sa.ForeignKey("connector_accounts.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("access_token_ciphertext", sa.String(length=2048), nullable=False),
        sa.Column("refresh_token_ciphertext", sa.String(length=2048), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("scopes", sa.JSON(), nullable=False),
        sa.Column("account_label", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    op.create_table(
        "connector_oauth_pending",
        sa.Column("state", sa.String(length=128), primary_key=True),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("code_verifier", sa.String(length=256), nullable=False),
        sa.Column("redirect_uri", sa.String(length=512), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_connector_oauth_pending_workspace",
        "connector_oauth_pending",
        ["workspace_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_connector_oauth_pending_workspace",
        table_name="connector_oauth_pending",
    )
    op.drop_table("connector_oauth_pending")
    op.drop_table("connector_oauth_tokens")
