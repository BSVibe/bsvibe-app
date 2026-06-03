"""oauth_authorization_server — embed OAuth 2.0 + PKCE in bsvibe-app (Lift D1).

Four tables (Identity context, ``backend.identity.oauth_db``):

* ``oauth_clients`` — registered OAuth clients (RFC 7591). v1 ships
  public-clients only; the ``client_type`` column is reserved for a
  future confidential-client / client_credentials grant.
* ``oauth_codes`` — single-use authorization codes (RFC 6749 §4.1) bound
  to a PKCE challenge (RFC 7636).
* ``oauth_access_tokens`` — source of truth for issued access tokens.
  The wire format is an ES256 JWT (verified by resource servers via
  JWKS) carrying this row's id as ``jti``; revocation + introspection
  consult this table.
* ``oauth_refresh_tokens`` — opaque refresh handles, sha256-hashed at
  rest.

No backfill required — the legacy auth.bsvibe.dev had no live OAuth
clients at decommission. The founder registers Claude Code via the new
Settings → Developer panel once D1 ships.

Revision ID: oauth_authorization_server
Revises: product_bootstrap
Create Date: 2026-06-03
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "oauth_authorization_server"
down_revision: Union[str, Sequence[str], None] = "product_bootstrap"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "oauth_clients",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "workspace_id",
            sa.Uuid(),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("client_id", sa.String(length=80), nullable=False, unique=True),
        sa.Column("client_name", sa.String(length=120), nullable=False),
        sa.Column(
            "client_type",
            sa.String(length=16),
            nullable=False,
            server_default="public",
        ),
        sa.Column("redirect_uris", sa.JSON(), nullable=False),
        sa.Column("allowed_scopes", sa.JSON(), nullable=False),
        sa.Column(
            "created_by_user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_oauth_clients_workspace_id",
        "oauth_clients",
        ["workspace_id"],
    )
    op.create_index(
        "ix_oauth_clients_client_id",
        "oauth_clients",
        ["client_id"],
    )

    op.create_table(
        "oauth_codes",
        sa.Column("code", sa.String(length=64), primary_key=True),
        sa.Column("client_id", sa.String(length=80), nullable=False),
        sa.Column(
            "user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "workspace_id",
            sa.Uuid(),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("scope", sa.JSON(), nullable=False),
        sa.Column("redirect_uri", sa.String(length=1024), nullable=False),
        sa.Column("code_challenge", sa.String(length=128), nullable=False),
        sa.Column(
            "code_challenge_method",
            sa.String(length=8),
            nullable=False,
            server_default="S256",
        ),
        sa.Column(
            "issued_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_oauth_codes_client_id",
        "oauth_codes",
        ["client_id"],
    )

    op.create_table(
        "oauth_access_tokens",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "workspace_id",
            sa.Uuid(),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("client_id", sa.String(length=80), nullable=False),
        sa.Column("scope", sa.JSON(), nullable=False),
        sa.Column(
            "issued_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("label", sa.String(length=120), nullable=True),
    )
    op.create_index(
        "ix_oauth_access_tokens_workspace_id",
        "oauth_access_tokens",
        ["workspace_id"],
    )
    op.create_index(
        "ix_oauth_access_tokens_user_id",
        "oauth_access_tokens",
        ["user_id"],
    )
    op.create_index(
        "ix_oauth_access_tokens_client_id",
        "oauth_access_tokens",
        ["client_id"],
    )

    op.create_table(
        "oauth_refresh_tokens",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "access_token_id",
            sa.Uuid(),
            sa.ForeignKey("oauth_access_tokens.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "token_hash",
            sa.LargeBinary(length=32),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "issued_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_oauth_refresh_tokens_access",
        "oauth_refresh_tokens",
        ["access_token_id"],
    )
    op.create_index(
        "ix_oauth_refresh_tokens_token_hash",
        "oauth_refresh_tokens",
        ["token_hash"],
    )


def downgrade() -> None:
    op.drop_index("ix_oauth_refresh_tokens_token_hash", table_name="oauth_refresh_tokens")
    op.drop_index("ix_oauth_refresh_tokens_access", table_name="oauth_refresh_tokens")
    op.drop_table("oauth_refresh_tokens")
    op.drop_index("ix_oauth_access_tokens_client_id", table_name="oauth_access_tokens")
    op.drop_index("ix_oauth_access_tokens_user_id", table_name="oauth_access_tokens")
    op.drop_index("ix_oauth_access_tokens_workspace_id", table_name="oauth_access_tokens")
    op.drop_table("oauth_access_tokens")
    op.drop_index("ix_oauth_codes_client_id", table_name="oauth_codes")
    op.drop_table("oauth_codes")
    op.drop_index("ix_oauth_clients_client_id", table_name="oauth_clients")
    op.drop_index("ix_oauth_clients_workspace_id", table_name="oauth_clients")
    op.drop_table("oauth_clients")
