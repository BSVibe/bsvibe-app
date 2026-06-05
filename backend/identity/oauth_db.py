"""OAuth 2.0 persistence — RFC 6749 + RFC 7636 (PKCE) + RFC 7591 (DCR).

Lift D1 — embed OAuth authorization server inside bsvibe-app, replacing
the dead ``auth.bsvibe.dev`` Next.js service. Identity context.

Four tables:

* ``oauth_clients`` — RFC 7591 dynamic-client-registration rows. v1 is
  public-clients only (PKCE-bound, no client_secret); ``client_type`` is
  retained for future confidential-client expansion.
* ``oauth_codes`` — single-use authorization codes bound to a client +
  redirect_uri + PKCE challenge. Atomically flipped to ``used_at`` on
  ``/token`` exchange so replay = ``invalid_grant``.
* ``oauth_access_tokens`` — issued access tokens (we mint a self-contained
  ES256 JWT for resource-server verification via JWKS, but persist the
  ``jti`` so ``/revoke`` + ``/introspect`` work).
* ``oauth_refresh_tokens`` — opaque refresh tokens, sha256-hashed so the
  raw value is never at rest. Single-use rotation lives in the token
  endpoint, not here.

Workspace scoping: an OAuth client is owned by a single workspace (the
workspace whose owner created it). Tokens carry the same ``workspace_id``
so the global ORM auto-filter scopes ``/oauth/clients`` listings without
any caller-side guard. The ``oauth_codes`` rows are NOT workspace-scoped
in the filter sense because the code-exchange happens before the client
has any session — but the row stores ``workspace_id`` so the issued
access token can be stamped without a second lookup.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Index,
    LargeBinary,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.data import Base


def _utcnow() -> datetime:
    return datetime.now(UTC)


class OAuthClientRow(Base):
    """An OAuth 2.0 client registered with this authorization server.

    v1 vocabulary: ``client_type='public'`` only — PKCE is mandatory and
    no client_secret is issued. The ``confidential`` shape is reserved
    for a future client_credentials grant (out of scope for D1).
    """

    __tablename__ = "oauth_clients"
    # Workspace scope on this table is *advisory*, NOT enforced by the
    # auto-filter. Two flows need to find clients across workspaces:
    #   1. Anonymous DCR (open RFC 7591 §3) writes rows with
    #      ``workspace_id IS NULL`` — the user binds a workspace at
    #      ``/authorize`` time on a real PWA session. The auto-filter's
    #      ``workspace_id == ws`` excludes NULL rows, breaking consent.
    #   2. ``/authorize`` + ``/token`` look the client up before the
    #      caller's workspace context is necessarily relevant.
    # Founder-facing listings (``list_clients_for_workspace``) still
    # apply an explicit ``WHERE workspace_id = ?`` so the Settings UI
    # stays workspace-isolated.
    __exclude_workspace_filter__ = True

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    # Nullable since Lift D2 followup: anonymous (RFC 7591 §3 open) DCR
    # rows are NOT bound to a workspace at registration — the USER binds
    # one later during ``/authorize`` (which runs on a real PWA session).
    # Founder-created rows from the Settings UI still populate this.
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=True, index=True
    )
    # RFC 7591 client_id — the externally-visible identifier. We prefix
    # ``dcr-`` to dynamically-registered clients so static seeding (none
    # in v1) and dynamic registration stay distinguishable.
    client_id: Mapped[str] = mapped_column(String(80), nullable=False, unique=True, index=True)
    # Human-readable name shown on the consent screen and in the
    # founder's "OAuth Clients" Settings panel.
    client_name: Mapped[str] = mapped_column(String(120), nullable=False)
    client_type: Mapped[str] = mapped_column(String(16), nullable=False, default="public")
    redirect_uris: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    allowed_scopes: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    # Nullable since Lift D2 followup — anonymous DCR has no authenticated
    # caller to attribute. Founder-created rows still populate this.
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class OAuthCodeRow(Base):
    """Single-use authorization code (RFC 6749 §4.1.2, RFC 7636 PKCE).

    NOT workspace-scoped by the auto-filter — the code-exchange happens
    on the unauthenticated ``/token`` surface. We persist ``workspace_id``
    so the issued token can be stamped without a join.
    """

    __tablename__ = "oauth_codes"
    __exclude_workspace_filter__ = True

    code: Mapped[str] = mapped_column(String(64), primary_key=True)
    client_id: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    scope: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    redirect_uri: Mapped[str] = mapped_column(String(1024), nullable=False)
    code_challenge: Mapped[str] = mapped_column(String(128), nullable=False)
    code_challenge_method: Mapped[str] = mapped_column(String(8), nullable=False, default="S256")
    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class OAuthAccessTokenRow(Base):
    """An access token we issued. Source of truth for revocation +
    introspection.

    The wire-format access_token is an ES256 JWT containing ``jti`` =
    this row's id. Resource servers (D2's MCP) verify via JWKS without a
    round-trip; ``/oauth/introspect`` consults this row.
    """

    __tablename__ = "oauth_access_tokens"
    __exclude_workspace_filter__ = True

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    client_id: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    scope: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Optional human label: ``client:<client_name>`` for /token issuance,
    # ``pat:<user-chosen-name>`` for founder-created PATs.
    label: Mapped[str | None] = mapped_column(String(120), nullable=True)


class OAuthRefreshTokenRow(Base):
    """Opaque refresh token, sha256-hashed at rest.

    Refresh tokens are not JWTs — they're high-entropy random strings
    issued alongside an access token. Storing only the hash means a DB
    leak doesn't yield usable tokens. Single-use rotation is implemented
    in the ``/token`` endpoint by atomic ``used_at`` flip.
    """

    __tablename__ = "oauth_refresh_tokens"
    __table_args__ = (Index("ix_oauth_refresh_tokens_access", "access_token_id"),)
    __exclude_workspace_filter__ = True

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    access_token_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("oauth_access_tokens.id", ondelete="CASCADE"),
        nullable=False,
    )
    token_hash: Mapped[bytes] = mapped_column(
        LargeBinary(32), nullable=False, unique=True, index=True
    )
    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


__all__ = [
    "OAuthAccessTokenRow",
    "OAuthClientRow",
    "OAuthCodeRow",
    "OAuthRefreshTokenRow",
]
