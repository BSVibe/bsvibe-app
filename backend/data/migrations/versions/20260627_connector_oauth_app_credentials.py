"""connector_oauth_app_credentials — instance-global OAuth App creds (Lift 1.5).

The GitHub App Manifest flow (design §, Lift 1.5) lets a founder create the
bsvibe GitHub App from the PWA; the minted credentials (client_id/secret,
app_id, private key PEM, webhook secret) are stored here — one row per provider
(instance-global; per-workspace tokens hang off the App, not per-workspace
apps). Secrets are encrypted via CredentialCipher.

Revision ID: connector_oauth_app_credentials
Revises: connector_oauth_tokens
Create Date: 2026-06-05
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "connector_oauth_app_credentials"
down_revision: Union[str, Sequence[str], None] = "connector_oauth_tokens"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "connector_oauth_app_credentials",
        sa.Column("provider", sa.String(length=64), primary_key=True),
        sa.Column("app_id", sa.String(length=64), nullable=False),
        sa.Column("app_slug", sa.String(length=255), nullable=True),
        sa.Column("client_id", sa.String(length=255), nullable=False),
        sa.Column("client_secret_ciphertext", sa.String(length=2048), nullable=False),
        sa.Column("private_key_pem_ciphertext", sa.String(length=8192), nullable=False),
        sa.Column("webhook_secret_ciphertext", sa.String(length=2048), nullable=True),
        sa.Column("html_url", sa.String(length=512), nullable=True),
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


def downgrade() -> None:
    op.drop_table("connector_oauth_app_credentials")
