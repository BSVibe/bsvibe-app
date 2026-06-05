"""Connector OAuth service layer — shared by the REST routes and MCP tools.

The browser-redirect callbacks stay in the route module (they're inherently
HTTP), but the *initiation* + *status* logic is identical whether a founder
clicks a PWA button or an MCP agent calls a tool, so it lives here once to
avoid drift. bsvibe acts as an OAuth client of the third party throughout
(redirect_uri is always the CONFIGURED issuer base, never a request host).
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import get_settings
from backend.connectors.auth import store
from backend.connectors.auth.app_credentials import get_app_credentials
from backend.connectors.auth.github_manifest import build_manifest, manifest_post_url
from backend.connectors.auth.providers import get_provider
from backend.router.accounts.crypto import CredentialCipher

# Pending-row marker for the App Manifest flow's CSRF state — distinct from the
# user-OAuth ``github`` pending rows so the two never collide.
MANIFEST_PENDING_PROVIDER = "github:app-manifest"  # noqa: S105 — marker, not a secret

_STATE_BYTES = 32
_VERIFIER_BYTES = 48


class UnknownProviderError(Exception):
    """Raised when an OAuth connect is requested for an unregistered provider."""


def _issuer() -> str:
    return get_settings().oauth_issuer.rstrip("/")


def callback_redirect_uri(provider: str) -> str:
    """The CONFIGURED callback URL the provider redirects back to."""
    return f"{_issuer()}/api/v1/connectors/oauth/{provider}/callback"


def manifest_redirect_uri() -> str:
    return f"{_issuer()}/api/v1/connectors/oauth/github/app-manifest/callback"


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(_VERIFIER_BYTES)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


async def begin_oauth_connect(
    session: AsyncSession, *, provider: str, workspace_id: uuid.UUID
) -> str:
    """Stash CSRF state + PKCE, return the provider authorize URL.

    Raises :class:`UnknownProviderError` when the provider isn't registered
    (its App credentials aren't configured).
    """
    prov = get_provider(provider)
    if prov is None:
        raise UnknownProviderError(provider)
    state = secrets.token_urlsafe(_STATE_BYTES)
    verifier, challenge = _pkce_pair()
    redirect_uri = callback_redirect_uri(provider)
    await store.create_pending(
        session,
        state=state,
        provider=provider,
        workspace_id=workspace_id,
        code_verifier=verifier,
        redirect_uri=redirect_uri,
    )
    await session.commit()
    return prov.authorize_url(state=state, code_challenge=challenge, redirect_uri=redirect_uri)


async def begin_github_app_manifest(
    session: AsyncSession, *, workspace_id: uuid.UUID
) -> dict[str, Any]:
    """Stash CSRF state, return the GitHub POST target + manifest body."""
    settings = get_settings()
    state = secrets.token_urlsafe(_STATE_BYTES)
    redirect_uri = manifest_redirect_uri()
    await store.create_pending(
        session,
        state=state,
        provider=MANIFEST_PENDING_PROVIDER,
        workspace_id=workspace_id,
        code_verifier="-",
        redirect_uri=redirect_uri,
    )
    await session.commit()
    manifest = build_manifest(
        homepage_url=settings.pwa_url.rstrip("/"),
        redirect_url=redirect_uri,
        oauth_callback_url=callback_redirect_uri("github"),
        webhook_url=f"{_issuer()}/api/webhooks/github",
    )
    return {"post_url": manifest_post_url(state), "manifest": manifest}


async def compute_github_app_status(
    session: AsyncSession, *, cipher: CredentialCipher
) -> dict[str, Any]:
    """Whether the GitHub App is set up + (DB-minted) slug/url."""
    creds = await get_app_credentials(session, provider="github", cipher=cipher)
    configured = get_provider("github") is not None or creds is not None
    return {
        "configured": configured,
        "app_slug": creds.app_slug if creds else None,
        "html_url": creds.html_url if creds else None,
    }


__all__ = [
    "MANIFEST_PENDING_PROVIDER",
    "UnknownProviderError",
    "begin_github_app_manifest",
    "begin_oauth_connect",
    "callback_redirect_uri",
    "compute_github_app_status",
    "manifest_redirect_uri",
]
