"""connector_accounts — workspace-scoped inbound webhook bindings (§11.2).

One row per external connector a workspace registered for inbound webhooks.
``webhook_token`` is the unguessable path component an external provider
calls (UNIQUE → resolves to one workspace); ``signing_secret_ciphertext``
holds the per-account signing secret encrypted via CredentialCipher (no
plaintext secret on disk), mirroring ``model_accounts.api_key_encrypted``.

Revision ID: connector_accounts
Revises: settle_drains
Create Date: 2026-05-30
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "connector_accounts"
down_revision: Union[str, Sequence[str], None] = "settle_drains"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "connector_accounts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("connector", sa.String(64), nullable=False),
        sa.Column("webhook_token", sa.String(128), nullable=False),
        sa.Column("signing_secret_ciphertext", sa.String(1024), nullable=False),
        sa.Column("external_ref", sa.String(255), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("webhook_token", name="uq_connector_accounts_webhook_token"),
    )
    op.create_index("ix_connector_accounts_workspace_id", "connector_accounts", ["workspace_id"])
    op.create_index(
        "ix_connector_accounts_lookup",
        "connector_accounts",
        ["connector", "webhook_token"],
    )


def downgrade() -> None:
    op.drop_index("ix_connector_accounts_lookup", table_name="connector_accounts")
    op.drop_index("ix_connector_accounts_workspace_id", table_name="connector_accounts")
    op.drop_table("connector_accounts")
