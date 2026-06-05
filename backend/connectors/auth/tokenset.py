"""TokenSet — provider-agnostic result of any token acquisition.

Every :class:`backend.connectors.auth.providers.OAuthProvider` method that
yields credentials (``exchange_code`` / ``refresh`` / ``service_token``)
returns this one shape, so the storage + resolution layers never branch on
which provider produced it. ``refresh_token`` / ``expires_at`` are optional —
some providers (GitHub OAuth App, Slack without rotation) issue non-expiring
tokens with no refresh material.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class TokenSet:
    """A normalized credential bundle returned by an OAuth provider."""

    access_token: str
    refresh_token: str | None = None
    expires_at: datetime | None = None
    scopes: tuple[str, ...] = field(default_factory=tuple)
    # Human-facing identity of the connected account ("@octocat", workspace
    # name) — surfaced in the UI as "Connected as …". ``None`` when the
    # provider does not report one.
    account_label: str | None = None


__all__ = ["TokenSet"]
