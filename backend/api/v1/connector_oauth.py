"""/api/v1/connectors/oauth/{provider}/{start,callback} — OAuth connect flow.

bsvibe acting as an OAuth *client* of a third party (the opposite direction
from :mod:`backend.identity.oauth_service`). Two endpoints:

* ``POST /{provider}/start`` (founder-authed) — mint a single-use CSRF
  ``state`` + PKCE pair, stash them server-side, and return the provider's
  authorize URL. The ``redirect_uri`` is built from the CONFIGURED backend
  base (``settings.oauth_issuer``), never the inbound request host — deriving
  it from the request is the redirect_uri trap (see skills
  nextjs-middleware-origin-trap / oauth-loopback-redirect-uri-strict-equal-trap).

* ``GET /{provider}/callback`` (public — the provider redirects the browser
  here) — claim the pending row (single-use → replay fails), exchange the
  code, persist an encrypted token linked to a ``connector_accounts`` binding,
  and 302 the browser back to the PWA settings.

Lift 0 ships only the StubProvider; real providers register from Lift 1.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import uuid
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import get_db_session, get_workspace_id
from backend.api.webhooks import get_credential_cipher
from backend.config import get_settings
from backend.connectors.auth import store
from backend.connectors.auth.app_credentials import upsert_app_credentials
from backend.connectors.auth.bootstrap import load_app_credential_providers
from backend.connectors.auth.github_manifest import (
    build_manifest,
    convert_manifest_code,
    manifest_post_url,
)
from backend.connectors.auth.providers import get_provider
from backend.router.accounts.crypto import CredentialCipher

logger = structlog.get_logger(__name__)

# Pending-row marker for the App Manifest flow's CSRF state — distinct from the
# user-OAuth ``github`` pending rows so the two never collide.
_MANIFEST_PENDING_PROVIDER = "github:app-manifest"  # noqa: S105 — marker, not a secret

# ``router`` carries the founder-authed ``/start`` and is mounted under the
# auth-gated v1 router (/api/v1/connectors/oauth). ``public_router`` carries
# the ``/callback`` and is mounted OUTSIDE the auth gate (the third party's
# browser redirect has no bsvibe session) — same split as webhooks + the
# identity OAuth public endpoints.
router = APIRouter()
public_router = APIRouter()

_STATE_BYTES = 32
_VERIFIER_BYTES = 48


def _pkce_pair() -> tuple[str, str]:
    """Return ``(code_verifier, code_challenge)`` for PKCE S256."""
    verifier = secrets.token_urlsafe(_VERIFIER_BYTES)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _callback_redirect_uri(provider: str) -> str:
    """The CONFIGURED callback URL the provider redirects back to."""
    base = get_settings().oauth_issuer.rstrip("/")
    return f"{base}/api/v1/connectors/oauth/{provider}/callback"


def _pwa_return_url(provider: str, *, ok: bool) -> str:
    base = get_settings().pwa_url.rstrip("/")
    key = "connected" if ok else "connect_error"
    return f"{base}/settings/connectors?{key}={provider}"


def _manifest_redirect_uri() -> str:
    """Where GitHub returns the manifest creation ``code`` (CONFIGURED base)."""
    base = get_settings().oauth_issuer.rstrip("/")
    return f"{base}/api/v1/connectors/oauth/github/app-manifest/callback"


def _github_webhook_base() -> str:
    base = get_settings().oauth_issuer.rstrip("/")
    return f"{base}/api/webhooks/github"


def _pwa_manifest_return_url(*, ok: bool) -> str:
    base = get_settings().pwa_url.rstrip("/")
    key = "github_app" if ok else "github_app_error"
    value = "ready" if ok else "1"
    return f"{base}/settings/connectors?{key}={value}"


@router.post("/{provider}/start")
async def start_oauth(
    provider: str,
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict[str, str]:
    """Begin a connect: stash CSRF+PKCE, return the provider authorize URL."""
    prov = get_provider(provider)
    if prov is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"unknown or unregistered provider: {provider}",
        )
    state = secrets.token_urlsafe(_STATE_BYTES)
    verifier, challenge = _pkce_pair()
    redirect_uri = _callback_redirect_uri(provider)
    await store.create_pending(
        session,
        state=state,
        provider=provider,
        workspace_id=workspace_id,
        code_verifier=verifier,
        redirect_uri=redirect_uri,
    )
    await session.commit()
    return {
        "authorize_url": prov.authorize_url(
            state=state, code_challenge=challenge, redirect_uri=redirect_uri
        )
    }


@public_router.get("/connectors/oauth/{provider}/callback")
async def oauth_callback(
    provider: str,
    code: str,
    state: str,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    cipher: Annotated[CredentialCipher, Depends(get_credential_cipher)],
) -> RedirectResponse:
    """Complete a connect: exchange code, persist encrypted token, redirect."""
    prov = get_provider(provider)
    if prov is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"unknown or unregistered provider: {provider}",
        )
    pending = await store.claim_pending(session, state=state, provider=provider)
    if pending is None:
        # Unknown / expired / replayed state — CSRF defense.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid or expired state",
        )

    token = await prov.exchange_code(
        code=code,
        code_verifier=pending.code_verifier,
        redirect_uri=pending.redirect_uri,
    )
    account = await store.get_or_create_account(
        session,
        workspace_id=pending.workspace_id,
        connector=provider,
        cipher=cipher,
    )
    await store.upsert_token(
        session,
        connector_account_id=account.id,
        provider=provider,
        token=token,
        cipher=cipher,
    )
    await session.commit()
    logger.info(
        "connector_oauth_connected",
        provider=provider,
        workspace_id=str(pending.workspace_id),
        account_label=token.account_label,
    )
    return RedirectResponse(
        _pwa_return_url(provider, ok=True),
        status_code=status.HTTP_302_FOUND,
    )


# ── GitHub App Manifest flow (Lift 1.5) ────────────────────────────────


@router.post("/github/app-manifest/start")
async def start_github_app_manifest(
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict[str, object]:
    """Begin App creation: stash CSRF state, return the GitHub POST target + manifest.

    The PWA renders a hidden, auto-submitting form that POSTs ``manifest`` (JSON)
    to ``post_url``. GitHub creates the App, then redirects to the manifest's
    ``redirect_url`` with a ``code`` (and the echoed ``state``).
    """
    settings = get_settings()
    state = secrets.token_urlsafe(_STATE_BYTES)
    redirect_uri = _manifest_redirect_uri()
    await store.create_pending(
        session,
        state=state,
        provider=_MANIFEST_PENDING_PROVIDER,
        workspace_id=workspace_id,
        code_verifier="-",  # manifest flow has no PKCE; column is NOT NULL
        redirect_uri=redirect_uri,
    )
    await session.commit()
    manifest = build_manifest(
        homepage_url=settings.pwa_url.rstrip("/"),
        redirect_url=redirect_uri,
        oauth_callback_url=_callback_redirect_uri("github"),
        webhook_url=_github_webhook_base(),
    )
    return {"post_url": manifest_post_url(state), "manifest": manifest}


@public_router.get("/connectors/oauth/github/app-manifest/callback")
async def github_app_manifest_callback(
    code: str,
    state: str,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    cipher: Annotated[CredentialCipher, Depends(get_credential_cipher)],
) -> RedirectResponse:
    """Complete App creation: exchange the code, store creds, register provider."""
    pending = await store.claim_pending(
        session, state=state, provider=_MANIFEST_PENDING_PROVIDER
    )
    if pending is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid or expired state",
        )

    result = await convert_manifest_code(code)
    await upsert_app_credentials(
        session,
        provider="github",
        app_id=result.app_id,
        app_slug=result.app_slug,
        client_id=result.client_id,
        client_secret=result.client_secret,
        private_key_pem=result.private_key_pem,
        webhook_secret=result.webhook_secret,
        html_url=result.html_url,
        cipher=cipher,
    )
    await session.commit()
    # Register the GitHubAppProvider now so "Connect with GitHub" works without
    # a restart (DB load also re-registers it on the next boot).
    await load_app_credential_providers(session, cipher)
    logger.info(
        "github_app_manifest_created",
        app_id=result.app_id,
        app_slug=result.app_slug,
        workspace_id=str(pending.workspace_id),
    )
    return RedirectResponse(
        _pwa_manifest_return_url(ok=True),
        status_code=status.HTTP_302_FOUND,
    )


__all__ = ["public_router", "router"]
