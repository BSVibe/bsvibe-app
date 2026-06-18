"""OAuth token + pending-state persistence (connector AuthStrategy, Lift 0).

Two tables, kept separate from ``connector_accounts`` (the webhook binding):

* :class:`ConnectorOAuthTokenRow` — the encrypted OAuth material for one
  binding. 1:1 with a ``connector_accounts`` row (FK + unique). A connector
  can hold BOTH a webhook signing secret (on ``connector_accounts``) AND an
  OAuth token (here), so the OAuth material lives in its own row rather than
  as nullable columns on the binding. Access token is mandatory; refresh +
  expiry are nullable because some providers issue non-expiring tokens
  (GitHub OAuth App, Slack without rotation). Tokens are encrypted via the
  same :class:`backend.router.accounts.crypto.CredentialCipher` as every
  other secret — plaintext never touches disk.

* :class:`ConnectorOAuthPendingRow` — the short-lived CSRF ``state`` + PKCE
  ``code_verifier`` held between the ``/start`` redirect and the
  ``/callback``. Single-use + TTL-reaped. Deliberately has NO FK to
  ``connector_accounts``: for an OAuth-first connect the account may not
  exist until the callback creates it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Index,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.data import Base


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


class ConnectorOAuthTokenRow(Base):
    """Encrypted OAuth token material for one connector binding (1:1)."""

    __tablename__ = "connector_oauth_tokens"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    # 1:1 with the webhook binding. CASCADE: dropping the binding drops its
    # token. Unique enforces the one-token-per-binding invariant.
    connector_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("connector_accounts.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    access_token_ciphertext: Mapped[str] = mapped_column(String(2048), nullable=False)
    refresh_token_ciphertext: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    scopes: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    account_label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )
    # Lift E46 — health state. ``active`` for a working OAuth-bound row;
    # ``needs_reauth`` when ``resolve_connector_credentials`` caught the
    # refresh failure and persisted the signal. The PWA reads this through
    # the connectors API to render a "Reconnect" CTA instead of a stale
    # "connected" badge.
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="active", server_default="active"
    )


#: Lift E46 — full enum of values the ``status`` column can carry.
TOKEN_STATUS_VALUES = ("active", "needs_reauth")


class ConnectorOAuthAppCredentialRow(Base):
    """Instance-global OAuth *App* credentials for one provider (1 row each).

    bsvibe acts as an OAuth client of a third party; that requires a registered
    App (client_id/secret + — for GitHub Apps — app_id + a private key). These
    are NOT per-workspace (the standard SaaS pattern is one app, per-workspace
    *tokens*), so ``provider`` is the primary key. Populated either by the
    GitHub App Manifest flow (founder clicks "Set up GitHub App", GitHub mints
    everything and we store it) or, as a fallback, from env settings. Every
    secret is encrypted via :class:`backend.router.accounts.crypto.CredentialCipher`.
    """

    __tablename__ = "connector_oauth_app_credentials"

    provider: Mapped[str] = mapped_column(String(64), primary_key=True)
    app_id: Mapped[str] = mapped_column(String(64), nullable=False)
    app_slug: Mapped[str | None] = mapped_column(String(255), nullable=True)
    client_id: Mapped[str] = mapped_column(String(255), nullable=False)
    client_secret_ciphertext: Mapped[str] = mapped_column(String(2048), nullable=False)
    private_key_pem_ciphertext: Mapped[str] = mapped_column(String(8192), nullable=False)
    webhook_secret_ciphertext: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    html_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )


class ConnectorOAuthPendingRow(Base):
    """Short-lived CSRF state + PKCE verifier for an in-flight OAuth connect."""

    __tablename__ = "connector_oauth_pending"
    __table_args__ = (Index("ix_connector_oauth_pending_workspace", "workspace_id"),)

    # The opaque, single-use ``state`` is the lookup key returned by the
    # provider on callback (CSRF binding).
    state: Mapped[str] = mapped_column(String(128), primary_key=True)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    workspace_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    code_verifier: Mapped[str] = mapped_column(String(256), nullable=False)
    redirect_uri: Mapped[str] = mapped_column(String(512), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class ConnectorOAuthUnclaimedRow(Base):
    """An exchanged OAuth token awaiting a workspace claim (claim-later).

    Sentry's install→grant redirect carries no ``state``, so the callback can't
    bind the token to a workspace. It exchanges the grant code (the code is
    short-lived) and parks the resulting token here, unbound; the founder then
    claims it for a workspace via an authenticated call. ``installation_ref`` is
    the Sentry ``installationId`` (needed for later refresh). Encrypted at rest.
    """

    __tablename__ = "connector_oauth_unclaimed"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    installation_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    account_label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    access_token_ciphertext: Mapped[str] = mapped_column(String(2048), nullable=False)
    refresh_token_ciphertext: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


__all__ = [
    "ConnectorOAuthAppCredentialRow",
    "ConnectorOAuthPendingRow",
    "ConnectorOAuthTokenRow",
    "ConnectorOAuthUnclaimedRow",
]
