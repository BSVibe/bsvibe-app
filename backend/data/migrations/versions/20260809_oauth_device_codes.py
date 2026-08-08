"""oauth_device_codes — RFC 8628 device authorization grant.

A pending device authorization holds two codes with different audiences:
``device_code`` (polled by the CLI, stored ONLY as a sha256 hash so a database
leak yields nothing exchangeable) and ``user_code`` (retyped by a human in a
browser, short and stored in the clear — useless without that human's own
authenticated session).

Additive: a new table, no existing column touched, so the reverse migration is
a plain drop.

Revision ID: oauth_device_codes
Revises: oauth_token_nullable_expiry
Create Date: 2026-08-09
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "oauth_device_codes"
down_revision: Union[str, Sequence[str], None] = "oauth_token_nullable_expiry"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "oauth_device_codes",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("client_id", sa.String(length=80), nullable=False),
        sa.Column("device_code_hash", sa.LargeBinary(length=32), nullable=False),
        sa.Column("user_code", sa.String(length=16), nullable=False),
        sa.Column("scope", sa.JSON(), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_polled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("denied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "workspace_id",
            sa.Uuid(),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_oauth_device_codes_client_id", "oauth_device_codes", ["client_id"])
    # Unique: the exchange looks up by hash, and a collision would let one
    # device's poll resolve another's request.
    op.create_index(
        "ix_oauth_device_codes_device_code_hash",
        "oauth_device_codes",
        ["device_code_hash"],
        unique=True,
    )
    # Unique: two live requests sharing a user_code would make the human's
    # approval ambiguous.
    op.create_index(
        "ix_oauth_device_codes_user_code", "oauth_device_codes", ["user_code"], unique=True
    )


def downgrade() -> None:
    op.drop_index("ix_oauth_device_codes_user_code", table_name="oauth_device_codes")
    op.drop_index("ix_oauth_device_codes_device_code_hash", table_name="oauth_device_codes")
    op.drop_index("ix_oauth_device_codes_client_id", table_name="oauth_device_codes")
    op.drop_table("oauth_device_codes")
