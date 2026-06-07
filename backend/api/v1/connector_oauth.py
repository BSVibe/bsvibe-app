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

import uuid
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import get_db_session, get_workspace_id
from backend.api.webhooks import get_credential_cipher
from backend.config import get_settings
from backend.connectors.auth import service, store
from backend.connectors.auth.app_credentials import upsert_app_credentials
from backend.connectors.auth.bootstrap import load_app_credential_providers
from backend.connectors.auth.github_manifest import convert_manifest_code
from backend.connectors.auth.providers import get_provider
from backend.connectors.auth.service import (
    MANIFEST_PENDING_PROVIDER as _MANIFEST_PENDING_PROVIDER,
)
from backend.router.accounts.crypto import CredentialCipher

logger = structlog.get_logger(__name__)

# ``router`` carries the founder-authed ``/start`` and is mounted under the
# auth-gated v1 router (/api/v1/connectors/oauth). ``public_router`` carries
# the ``/callback`` and is mounted OUTSIDE the auth gate (the third party's
# browser redirect has no bsvibe session) — same split as webhooks + the
# identity OAuth public endpoints.
router = APIRouter()
public_router = APIRouter()


def _pwa_return_url(provider: str, *, ok: bool) -> str:
    base = get_settings().pwa_url.rstrip("/")
    key = "connected" if ok else "connect_error"
    return f"{base}/settings/connectors?{key}={provider}"


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
    try:
        authorize_url = await service.begin_oauth_connect(
            session, provider=provider, workspace_id=workspace_id
        )
    except service.UnknownProviderError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"unknown or unregistered provider: {provider}",
        ) from exc
    return {"authorize_url": authorize_url}


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


@router.get("/github/app-status")
async def github_app_status(
    session: Annotated[AsyncSession, Depends(get_db_session)],
    cipher: Annotated[CredentialCipher, Depends(get_credential_cipher)],
) -> dict[str, object]:
    """Whether the GitHub App is set up — drives Set-up vs Connect in the UI.

    ``configured`` is true once a provider is registered (env or manifest-minted
    DB creds). ``app_slug`` / ``html_url`` come from the DB creds (None when the
    App was configured via env only).
    """
    return await service.compute_github_app_status(session, cipher=cipher)


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
    return await service.begin_github_app_manifest(session, workspace_id=workspace_id)


class AppCredentialsIn(BaseModel):
    """Operator-pasted OAuth App credentials for a vanilla provider."""

    model_config = ConfigDict(extra="forbid")
    client_id: str = Field(..., min_length=1, max_length=255)
    client_secret: str = Field(..., min_length=1, max_length=1024)


@router.post("/{provider}/app-credentials")
async def set_provider_app_credentials(
    provider: str,
    payload: AppCredentialsIn,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    cipher: Annotated[CredentialCipher, Depends(get_credential_cipher)],
) -> dict[str, object]:
    """Operator: store a vanilla provider's (slack/notion/discord) App creds.

    The operator creates the OAuth app in the provider's console (no manifest
    API there) and pastes client_id/secret here — stored encrypted + the
    provider registers so workspaces can 1-click connect. github uses the
    manifest flow, not this (→ 400).
    """
    try:
        await service.set_app_credentials(
            session,
            provider=provider,
            client_id=payload.client_id,
            client_secret=payload.client_secret,
            cipher=cipher,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return {"provider": provider, "configured": True}


@public_router.get("/connectors/oauth/github/app-manifest/callback")
async def github_app_manifest_callback(
    code: str,
    state: str,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    cipher: Annotated[CredentialCipher, Depends(get_credential_cipher)],
) -> RedirectResponse:
    """Complete App creation: exchange the code, store creds, register provider."""
    pending = await store.claim_pending(session, state=state, provider=_MANIFEST_PENDING_PROVIDER)
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


# ── Unclaimed installs (claim-later) ────────────────────────────────────


@router.get("/unclaimed")
async def list_unclaimed_installs(
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict[str, object]:
    """Installs awaiting a workspace claim (e.g. Sentry). No secrets returned."""
    return {"unclaimed": await service.list_unclaimed_installs(session)}


@router.post("/unclaimed/{unclaimed_id}/claim")
async def claim_unclaimed_install(
    unclaimed_id: uuid.UUID,
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    cipher: Annotated[CredentialCipher, Depends(get_credential_cipher)],
) -> dict[str, object]:
    """Bind an unclaimed install to the active workspace."""
    try:
        connector = await service.claim_install(
            session, unclaimed_id=unclaimed_id, workspace_id=workspace_id, cipher=cipher
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return {"connector": connector, "claimed": True}


# ── Sentry install→grant flow (claim-later, design §11) ─────────────────


@router.get("/sentry/install-url")
async def sentry_install_url(
    session: Annotated[AsyncSession, Depends(get_db_session)],
    cipher: Annotated[CredentialCipher, Depends(get_credential_cipher)],
) -> dict[str, object]:
    """The Sentry external-install URL (founder opens it to install + connect).

    ``configured`` false when the operator hasn't set the Sentry integration's
    creds + slug yet.
    """
    url = await service.sentry_install_url(session, cipher=cipher)
    return {"configured": url is not None, "install_url": url}


@public_router.get("/connectors/oauth/sentry/install/callback")
async def sentry_install_callback(
    code: str,
    installation_id: Annotated[str, Query(alias="installationId")],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    cipher: Annotated[CredentialCipher, Depends(get_credential_cipher)],
) -> RedirectResponse:
    """Sentry redirects here post-install with ``code`` + ``installationId``.

    No workspace binding (Sentry passes no state) — exchange the grant + park
    the token as an unclaimed install; the founder claims it afterwards.
    """
    try:
        await service.complete_sentry_install(
            session, code=code, installation_id=installation_id, cipher=cipher
        )
    except service.UnknownProviderError:
        return RedirectResponse(
            _pwa_return_url("sentry", ok=False), status_code=status.HTTP_302_FOUND
        )
    logger.info("sentry_install_unclaimed", installation_id=installation_id)
    base = get_settings().pwa_url.rstrip("/")
    return RedirectResponse(
        f"{base}/settings/connectors?sentry_install=pending",
        status_code=status.HTTP_302_FOUND,
    )


__all__ = ["public_router", "router"]
