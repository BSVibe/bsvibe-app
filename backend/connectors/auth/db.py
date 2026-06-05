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


__all__ = ["ConnectorOAuthPendingRow", "ConnectorOAuthTokenRow"]
