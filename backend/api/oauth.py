"""/api/oauth/* — RFC 6749 + 7636 (PKCE) + 7591 (DCR) + 7662 + 7009 + 8414.

Public (unauthenticated) endpoints under ``/api/oauth/`` + the two
``/.well-known/`` metadata documents — mounted in
:mod:`backend.api.main` directly, NOT under the JWT-gated v1 router,
because the OAuth surface IS the authentication surface.

The ``/authorize`` endpoint is split across two HTTP methods:

* ``GET`` is **unauthenticated** — a browser top-level navigation cannot
  carry an ``Authorization`` header, so requiring a Bearer here would
  hard-break every OAuth-based MCP client (Claude Code, MCP Inspector,
  IDE plugins). Instead it validates the OAuth params and 302-redirects
  the user agent to the PWA-hosted consent page at
  ``<pwa_url>/oauth/consent?<same query>``, where a real Supabase session
  IS available to the React app.
* ``POST`` stays **authenticated** — the PWA consent page calls it via
  ``fetch`` with the Supabase Bearer attached, then JS navigates the
  browser to the JSON response's ``redirect_to`` (a fetch can't follow a
  302 cross-origin to the client's loopback redirect URI, hence the JSON
  shape). For backwards compatibility with existing curl/tests, when
  the request's ``Accept`` header lacks ``application/json`` the route
  preserves the original 302 behaviour.

``/token`` is unauthenticated by design (clients send PKCE).

Two auth-gated endpoints live here too — they manage the founder's own
OAuth clients (RFC 7591 §3 lets the AS gate DCR; we require founder
auth). They use the ``v1_router`` exposed below, which is mounted under
``/api/v1/oauth`` from :mod:`backend.api.main`.
"""

from __future__ import annotations

import re
import time
import uuid
from datetime import datetime
from threading import Lock
from typing import Annotated, Any
from urllib.parse import urlencode, urlsplit

import structlog
from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import (
    CurrentUser,
    get_current_user_row,
    get_db_session,
    get_workspace_id,
)
from backend.config import get_settings
from backend.identity.db import UserRow
from backend.identity.oauth_clients_service import (
    list_clients_for_workspace,
    lookup_client_by_client_id,
    register_client,
    revoke_client,
)
from backend.identity.oauth_db import OAuthClientRow
from backend.identity.oauth_jwt import ACCESS_TOKEN_AUDIENCE
from backend.identity.oauth_keys import jwks_payload
from backend.identity.oauth_pkce import match_redirect_uri
from backend.identity.oauth_service import (
    CodeClaimOutcome,
    RefreshRotateOutcome,
    RevokeKind,
    claim_authorization_code,
    introspect_token,
    issue_authorization_code,
    issue_token_pair,
    revoke_token,
    rotate_refresh_token,
)

logger = structlog.get_logger(__name__)

# Public (no auth) — mounted at /api/oauth.
public_router = APIRouter(prefix="/oauth", tags=["oauth"])
# Authenticated founder management — mounted at /api/v1/oauth.
v1_router = APIRouter(prefix="/oauth", tags=["oauth"])
# RFC 8414 / RFC 9728 well-known metadata + JWKS — mounted at /api.
metadata_router = APIRouter(tags=["oauth-metadata"])


# Scopes the embedded OAuth server understands. MCP clients (D2) request
# these; the consent screen shows them; tokens carry them. Extending the
# set is additive — never remove.
ALLOWED_SCOPES = ("mcp:read", "mcp:write", "mcp:admin")
DEFAULT_SCOPE = "mcp:read"


# RFC 8252 loopback redirect URI — native clients (Claude Code,
# `claude mcp add`, etc.) listen on a random localhost port for the
# authorization-code callback. The anonymous DCR endpoint accepts ONLY
# these so the open-DCR surface can't be weaponised into an open redirect.
_LOOPBACK_REDIRECT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^http://127\.0\.0\.1(:[0-9]+)?(/.*)?$"),
    re.compile(r"^http://localhost(:[0-9]+)?(/.*)?$"),
)


def _is_loopback_redirect_uri(uri: str) -> bool:
    return any(p.match(uri) for p in _LOOPBACK_REDIRECT_PATTERNS)


# ---------------------------------------------------------------------------
# Anonymous DCR rate limiter
# ---------------------------------------------------------------------------
# Simple per-IP sliding-window counter. Used to throttle the unauthenticated
# ``POST /api/oauth/register`` endpoint so a flood of bot registrations
# can't pollute the client table. In-process / single-worker only — v1
# deliberately ships without a Redis dependency; if we later move to
# multi-worker uvicorn we can swap this for a Redis-backed counter.
_ANON_DCR_WINDOW_SECS = 3600
_ANON_DCR_MAX_PER_WINDOW = 10
_anon_dcr_lock = Lock()
_anon_dcr_buckets: dict[str, list[float]] = {}


def _anon_dcr_rate_check(ip: str, *, now: float | None = None) -> bool:
    """Return ``True`` if the IP may register one more client.

    Side-effect: on success, records ``now`` into the IP's bucket.
    Buckets older than the window are pruned lazily on every call.
    """
    t = time.monotonic() if now is None else now
    cutoff = t - _ANON_DCR_WINDOW_SECS
    with _anon_dcr_lock:
        bucket = [ts for ts in _anon_dcr_buckets.get(ip, ()) if ts > cutoff]
        if len(bucket) >= _ANON_DCR_MAX_PER_WINDOW:
            _anon_dcr_buckets[ip] = bucket
            return False
        bucket.append(t)
        _anon_dcr_buckets[ip] = bucket
        return True


def _reset_anon_dcr_rate_limit_for_tests() -> None:
    """Test-only: clear the per-IP buckets between cases."""
    with _anon_dcr_lock:
        _anon_dcr_buckets.clear()


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class _OAuthError(JSONResponse):
    """RFC 6749 §5.2 error body."""

    def __init__(
        self,
        status_code: int,
        error: str,
        description: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        payload: dict[str, str] = {"error": error}
        if description:
            payload["error_description"] = description
        super().__init__(payload, status_code=status_code, headers=headers)


class TokenResponse(BaseModel):
    """RFC 6749 §5.1 success body."""

    model_config = ConfigDict(extra="forbid")

    access_token: str
    token_type: str = "Bearer"  # noqa: S105 — OAuth wire-format label
    expires_in: int
    refresh_token: str | None = None
    scope: str


class ClientCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_name: str = Field(min_length=1, max_length=120)
    redirect_uris: list[str] = Field(min_length=1, max_length=10)
    allowed_scopes: list[str] | None = None


class ClientResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    client_id: str
    client_name: str
    client_type: str
    redirect_uris: list[str]
    allowed_scopes: list[str]
    created_at: datetime
    revoked_at: datetime | None


class AnonymousClientCreateRequest(BaseModel):
    """RFC 7591 §3 open DCR — Lift D2 followup.

    Tight constraints (vs the founder-authed v1 schema): max 100-char
    name, max 4 redirect URIs, **loopback-only** redirect_uris. PKCE is
    already enforced everywhere downstream.

    Accepts the full RFC 7591 §2 client-metadata vocabulary so spec-
    compliant SDKs (Claude Code, MCP Inspector, etc.) register without
    a 422. Fields not represented on our row are validated against
    server capabilities and then dropped — accepting them silently is
    the path RFC 7591 expects when the AS doesn't surface them in the
    response.
    """

    model_config = ConfigDict(extra="ignore")

    client_name: str = Field(min_length=1, max_length=100)
    redirect_uris: list[str] = Field(min_length=1, max_length=4)
    # Accept either the BSVibe-native `allowed_scopes` list or RFC 7591's
    # `scope` (space-separated string). The endpoint normalises both into
    # the row's scope list.
    allowed_scopes: list[str] | None = None
    scope: str | None = None
    # RFC 7591 §2 standard fields we validate against server capability.
    # We do NOT echo these back; the metadata document already tells the
    # client what the server supports.
    grant_types: list[str] | None = None
    response_types: list[str] | None = None
    token_endpoint_auth_method: str | None = None


class AnonymousClientResponse(BaseModel):
    """Response shape for ``POST /api/oauth/register``.

    Same fields as :class:`ClientResponse` minus the founder-only id
    columns; no ``client_secret`` (public clients only).
    """

    model_config = ConfigDict(extra="forbid")

    client_id: str
    client_name: str
    redirect_uris: list[str]
    allowed_scopes: list[str]
    created_at: datetime


def _client_to_response(row: OAuthClientRow) -> ClientResponse:
    return ClientResponse(
        id=row.id,
        client_id=row.client_id,
        client_name=row.client_name,
        client_type=row.client_type,
        redirect_uris=list(row.redirect_uris),
        allowed_scopes=list(row.allowed_scopes),
        created_at=row.created_at,
        revoked_at=row.revoked_at,
    )


# ---------------------------------------------------------------------------
# Authorization endpoint
# ---------------------------------------------------------------------------

# Pre-validated OAuth params + the resolved client row. Carried from the GET
# validator through the helper that builds the PWA-consent redirect URL, and
# (in a separate code path) from the POST validator into the code-mint step.
_AUTHORIZE_FORWARDED_PARAMS: tuple[str, ...] = (
    "response_type",
    "client_id",
    "redirect_uri",
    "scope",
    "state",
    "code_challenge",
    "code_challenge_method",
    "resource",
)


def _parse_scope(raw: str | None, allowed: list[str]) -> tuple[list[str], str | None]:
    """Split ``scope`` on whitespace and return (parsed, error-or-None).

    Empty / missing → ``DEFAULT_SCOPE``. Any requested scope not in
    ``allowed`` → error. Any scope not in :data:`ALLOWED_SCOPES` → error.
    """
    parts = (raw or DEFAULT_SCOPE).split()
    if not parts:
        parts = [DEFAULT_SCOPE]
    for s in parts:
        if s not in ALLOWED_SCOPES:
            return [], f"unknown scope: {s}"
        if s not in allowed:
            return [], f"scope not allowed for client: {s}"
    return parts, None


def _redirect_to_client_with_error(
    redirect_uri: str, state: str | None, error: str, description: str
) -> RedirectResponse:
    """RFC 6749 §4.1.2.1 — bounce a known-good ``redirect_uri`` an error."""
    params = {"error": error, "error_description": description}
    if state:
        params["state"] = state
    sep = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(
        f"{redirect_uri}{sep}{urlencode(params)}",
        status_code=302,
    )


def _client_bounce_url(redirect_uri: str, query: dict[str, str]) -> str:
    sep = "&" if "?" in redirect_uri else "?"
    return f"{redirect_uri}{sep}{urlencode(query)}"


def _pwa_consent_url(query: dict[str, str], *, error: str | None = None) -> str:
    """Build the PWA-side consent URL: ``<pwa_url>/oauth/consent?<query>``.

    When ``error`` is set, only the error params are forwarded — we never
    leak a half-validated ``client_id`` into a PWA error UI. The user can
    re-issue the flow from their MCP client.
    """
    pwa = get_settings().pwa_url.rstrip("/")
    if error is not None:
        return f"{pwa}/oauth/consent?{urlencode({'error': error, **query})}"
    return f"{pwa}/oauth/consent?{urlencode(query)}"


@public_router.get("/authorize", operation_id="oauth_authorize_get")
async def authorize_get(  # noqa: PLR0911 — OAuth validator
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> RedirectResponse:
    """RFC 6749 §3.1 — authorization endpoint, GET (consent entry).

    Unauthenticated by design. A browser top-level navigation cannot
    carry an ``Authorization`` header, so this endpoint validates the
    OAuth params and 302-redirects to the PWA-hosted consent page where
    the Supabase session IS available. The POST handler below — which
    the PWA calls via ``fetch`` after the founder clicks "Allow" — is
    the one that mints the authorization code.

    Errors:

    * Bad / unknown ``client_id``, missing required param, or invalid
      ``redirect_uri`` → 302 to ``<pwa>/oauth/consent?error=…`` so the
      PWA renders a clean explanation instead of a raw JSON 400.
    * After ``redirect_uri`` is known-good (registered for the client),
      protocol-level errors (missing PKCE, bad scope) 302 back to the
      client's redirect URI per RFC 6749 §4.1.2.1.
    """
    params = dict(request.query_params)
    response_type = params.get("response_type")
    client_id = params.get("client_id")
    redirect_uri = params.get("redirect_uri")
    scope = params.get("scope")
    state = params.get("state")
    code_challenge = params.get("code_challenge")
    code_challenge_method = params.get("code_challenge_method")

    if response_type != "code":
        return RedirectResponse(
            _pwa_consent_url({}, error="unsupported_response_type"),
            status_code=302,
        )
    if not client_id:
        return RedirectResponse(
            _pwa_consent_url({}, error="invalid_request"),
            status_code=302,
        )
    client = await lookup_client_by_client_id(session, client_id)
    if client is None or client.revoked_at is not None:
        return RedirectResponse(
            _pwa_consent_url({}, error="invalid_client"),
            status_code=302,
        )
    if not redirect_uri:
        return RedirectResponse(
            _pwa_consent_url({"client_id": client_id}, error="invalid_request"),
            status_code=302,
        )
    if not match_redirect_uri(list(client.redirect_uris), redirect_uri):
        return RedirectResponse(
            _pwa_consent_url({"client_id": client_id}, error="invalid_request"),
            status_code=302,
        )
    # Past this point we can safely 302 errors back to the client.
    if not code_challenge:
        return _redirect_to_client_with_error(
            redirect_uri, state, "invalid_request", "code_challenge is required"
        )
    if code_challenge_method != "S256":
        return _redirect_to_client_with_error(
            redirect_uri,
            state,
            "invalid_request",
            "code_challenge_method must be 'S256'",
        )
    _, scope_err = _parse_scope(scope, list(client.allowed_scopes))
    if scope_err is not None:
        return _redirect_to_client_with_error(redirect_uri, state, "invalid_scope", scope_err)

    # All params valid — forward to PWA consent page. The PWA will fetch
    # this client's metadata via /api/oauth/clients/by-client-id/{cid} and
    # render the Allow/Deny UI, then POST back here with the Supabase
    # bearer attached.
    forwarded = {k: params[k] for k in _AUTHORIZE_FORWARDED_PARAMS if params.get(k)}
    return RedirectResponse(_pwa_consent_url(forwarded), status_code=302)


def _wants_json(request: Request) -> bool:
    """True when the caller's ``Accept`` header lists ``application/json``.

    The PWA fetches POST /authorize and needs a JSON ``{redirect_to}``
    because a 302 cross-origin to a loopback port can't be followed by
    the browser fetch. Legacy curl / form-POST callers — and the existing
    test suite — keep the 302 behaviour.
    """
    accept = request.headers.get("accept", "")
    return "application/json" in accept.lower()


@public_router.post("/authorize", operation_id="oauth_authorize_post")
async def authorize_post(  # noqa: PLR0911 — OAuth state machine
    request: Request,
    user: CurrentUser,
    user_row: Annotated[UserRow, Depends(get_current_user_row)],
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> Any:
    """RFC 6749 §3.1 — authorization endpoint, POST (consent commit).

    Authenticated: the PWA consent page calls this with a Supabase
    bearer + the original OAuth params + ``action=approve|deny``. On
    approve we mint a single-use code; on deny we synthesise an
    ``access_denied`` error per §4.1.2.1.

    Response shape depends on the ``Accept`` header:

    * ``application/json`` → 200 with ``{"redirect_to": "<client_uri>"}``
      so a fetch can do ``window.location.href = redirect_to``.
    * anything else → 302 to the same URL (legacy curl + existing tests).
    """
    del user
    form = await request.form()
    params = {k: v for k, v in form.multi_items() if isinstance(v, str)}

    response_type = params.get("response_type")
    client_id = params.get("client_id")
    redirect_uri = params.get("redirect_uri")
    scope = params.get("scope")
    state = params.get("state")
    code_challenge = params.get("code_challenge")
    code_challenge_method = params.get("code_challenge_method")
    action = params.get("action")

    if response_type != "code" or not client_id:
        return _OAuthError(400, "invalid_request", "missing or invalid OAuth params")
    client = await lookup_client_by_client_id(session, client_id)
    if client is None or client.revoked_at is not None:
        return _OAuthError(400, "invalid_client", "unknown or revoked client")
    if not redirect_uri or not match_redirect_uri(list(client.redirect_uris), redirect_uri):
        return _OAuthError(400, "invalid_request", "redirect_uri is not registered")
    if not code_challenge or code_challenge_method != "S256":
        return _redirect_or_json(
            request,
            _redirect_to_client_with_error(
                redirect_uri,
                state,
                "invalid_request",
                "code_challenge / code_challenge_method missing or invalid",
            ),
        )
    parsed_scope, scope_err = _parse_scope(scope, list(client.allowed_scopes))
    if scope_err is not None:
        return _redirect_or_json(
            request,
            _redirect_to_client_with_error(redirect_uri, state, "invalid_scope", scope_err),
        )

    if action == "deny":
        return _redirect_or_json(
            request,
            _redirect_to_client_with_error(
                redirect_uri, state, "access_denied", "user denied the request"
            ),
        )
    if action != "approve":
        return _OAuthError(400, "invalid_request", "action must be 'approve' or 'deny'")

    # Approve — mint code. The OAuth subject is the consenting session user.
    code = await issue_authorization_code(
        session,
        client_id=client.client_id,
        user_id=user_row.id,
        workspace_id=workspace_id,
        scope=parsed_scope,
        redirect_uri=redirect_uri,
        code_challenge=code_challenge,
    )
    await session.commit()
    bounce: dict[str, str] = {"code": code}
    if state:
        bounce["state"] = state
    return _redirect_or_json(
        request,
        RedirectResponse(_client_bounce_url(redirect_uri, bounce), status_code=302),
    )


def _redirect_or_json(request: Request, redirect: RedirectResponse) -> Any:
    """Negotiate the response shape — JSON for fetch callers, 302 for the rest.

    The PWA consent page asks for JSON so it can do
    ``window.location.href = redirect_to`` (a cross-origin fetch can't
    follow a 302 to ``http://localhost:49921``). Curl + the legacy test
    suite keep the original 302 behaviour.
    """
    if _wants_json(request):
        return JSONResponse({"redirect_to": redirect.headers["location"]})
    return redirect


# ---------------------------------------------------------------------------
# Public client-info endpoint (powers the PWA consent screen)
# ---------------------------------------------------------------------------


class PublicClientResponse(BaseModel):
    """Public-facing OAuth client metadata for the consent screen.

    Intentionally a subset of :class:`ClientResponse` — no internal id,
    no ``created_at``, no revoked timestamp. The client_id is already in
    the URL the user is looking at, so exposing the human name + scope
    set + registered redirect URIs leaks nothing the OAuth client itself
    couldn't put on its own about page.
    """

    model_config = ConfigDict(extra="forbid")

    client_id: str
    client_name: str
    client_type: str
    redirect_uris: list[str]
    allowed_scopes: list[str]


@public_router.get(
    "/clients/by-client-id/{client_id}",
    response_model=PublicClientResponse,
    operation_id="oauth_public_client_lookup",
)
async def lookup_public_client(
    client_id: str,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> PublicClientResponse:
    """Return public client metadata so the PWA consent page can render
    "Allow {client_name}…".

    Unauthenticated by design — the ``client_id`` is already visible in
    the user-facing URL, and the returned data is what the OAuth client
    itself would advertise. Revoked + unknown both 404 (don't disclose
    that a row exists in a soft-revoked state).
    """
    row = await lookup_client_by_client_id(session, client_id)
    if row is None or row.revoked_at is not None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown client")
    return PublicClientResponse(
        client_id=row.client_id,
        client_name=row.client_name,
        client_type=row.client_type,
        redirect_uris=list(row.redirect_uris),
        allowed_scopes=list(row.allowed_scopes),
    )


# ---------------------------------------------------------------------------
# Token endpoint
# ---------------------------------------------------------------------------


@public_router.post("/token")
async def token(  # noqa: PLR0911 — OAuth state machine
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    grant_type: Annotated[str, Form()],
    client_id: Annotated[str, Form()],
    code: Annotated[str | None, Form()] = None,
    redirect_uri: Annotated[str | None, Form()] = None,
    code_verifier: Annotated[str | None, Form()] = None,
    refresh_token: Annotated[str | None, Form()] = None,
) -> Any:
    """RFC 6749 §4.1 + §6 — authorization_code + refresh_token grants."""
    del request
    settings = get_settings()
    issuer = settings.oauth_issuer
    if grant_type == "authorization_code":
        if not code:
            return _OAuthError(400, "invalid_request", "code is required")
        if not redirect_uri:
            return _OAuthError(400, "invalid_request", "redirect_uri is required")
        if not code_verifier:
            return _OAuthError(400, "invalid_request", "code_verifier is required")
        client = await lookup_client_by_client_id(session, client_id)
        if client is None or client.revoked_at is not None:
            return _OAuthError(401, "invalid_client", "unknown or revoked client")
        outcome, claimed = await claim_authorization_code(
            session,
            code=code,
            expected_client_id=client_id,
            redirect_uri=redirect_uri,
            code_verifier=code_verifier,
        )
        if outcome is not CodeClaimOutcome.CLAIMED or claimed is None:
            return _OAuthError(
                400,
                "invalid_grant",
                f"authorization code {outcome.value.replace('_', ' ')}",
            )
        pair = await issue_token_pair(
            session,
            user_id=claimed.user_id,
            workspace_id=claimed.workspace_id,
            client_id=claimed.client_id,
            scope=claimed.scope,
            issuer=issuer,
            label=f"oauth:{client.client_name}",
        )
        await session.commit()
        return TokenResponse(
            access_token=pair.access_token,
            expires_in=pair.expires_in,
            refresh_token=pair.refresh_token,
            scope=" ".join(pair.scope),
        )
    if grant_type == "refresh_token":
        if not refresh_token:
            return _OAuthError(400, "invalid_request", "refresh_token is required")
        rotate_outcome, rotated = await rotate_refresh_token(
            session,
            refresh_token=refresh_token,
            client_id=client_id,
            issuer=issuer,
        )
        if rotate_outcome is not RefreshRotateOutcome.ROTATED or rotated is None:
            return _OAuthError(401, "invalid_grant", rotate_outcome.value)
        await session.commit()
        return TokenResponse(
            access_token=rotated.access_token,
            expires_in=rotated.expires_in,
            refresh_token=rotated.refresh_token,
            scope=" ".join(rotated.scope),
        )
    return _OAuthError(
        400,
        "unsupported_grant_type",
        f"grant_type {grant_type} is not supported",
    )


# ---------------------------------------------------------------------------
# Introspect + revoke
# ---------------------------------------------------------------------------


@public_router.post("/introspect")
async def introspect(
    session: Annotated[AsyncSession, Depends(get_db_session)],
    token: Annotated[str, Form()],
    token_type_hint: Annotated[str | None, Form()] = None,
) -> dict[str, Any]:
    """RFC 7662 token introspection. ``active: false`` on any failure."""
    settings = get_settings()
    hint = (
        RevokeKind.REFRESH_TOKEN
        if token_type_hint == "refresh_token"  # noqa: S105
        else None
    )
    result = await introspect_token(session, token=token, issuer=settings.oauth_issuer, hint=hint)
    out: dict[str, Any] = {"active": result.active}
    if not result.active:
        return out
    for k in ("sub", "client_id", "scope", "exp", "iat", "token_type", "aud", "iss", "jti"):
        v = getattr(result, k)
        if v is not None:
            out[k] = v
    if result.workspace_id is not None:
        out["workspace_id"] = result.workspace_id
    return out


@public_router.post("/revoke", status_code=status.HTTP_200_OK)
async def revoke(
    session: Annotated[AsyncSession, Depends(get_db_session)],
    token: Annotated[str, Form()],
    token_type_hint: Annotated[str | None, Form()] = None,
) -> dict[str, bool]:
    """RFC 7009 — revoke an access or refresh token. Always 200."""
    settings = get_settings()
    hint = (
        RevokeKind.REFRESH_TOKEN
        if token_type_hint == "refresh_token"  # noqa: S105
        else None
    )
    revoked = await revoke_token(session, token=token, hint=hint, issuer=settings.oauth_issuer)
    await session.commit()
    return {"revoked": revoked}


# ---------------------------------------------------------------------------
# Anonymous DCR (RFC 7591 §3 open) — Lift D2 followup
# ---------------------------------------------------------------------------


@public_router.post(
    "/register",
    response_model=AnonymousClientResponse,
    status_code=status.HTTP_201_CREATED,
)
async def register_client_anonymous(  # noqa: PLR0912 — RFC 7591 §2 capability matrix
    request: Request,
    payload: AnonymousClientCreateRequest,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> AnonymousClientResponse:
    """RFC 7591 §3 — open dynamic client registration for MCP clients.

    Unauthenticated by design: native MCP clients (``claude mcp add``,
    Claude Code, etc.) discover this endpoint from the
    ``.well-known/oauth-authorization-server`` metadata BEFORE they hold
    any token. Abuse is constrained by:

    * **Loopback-only redirect URIs** (RFC 8252) — open registration
      cannot mint open-redirect clients to phishing-grade hostnames.
    * Per-IP rate limit (10/hour, in-process).
    * ``client_name`` ≤ 100 chars, ≤ 4 redirect URIs, scopes ⊆ server set.
    * Public client only — no ``client_secret`` returned. PKCE mandatory
      everywhere downstream.

    The row's ``workspace_id`` / ``created_by_user_id`` stay NULL. The
    *user* binds a workspace at ``/authorize`` time (which DOES run on a
    real PWA session).
    """
    # Resolve caller IP. Honors X-Forwarded-For if the ProxyHeaders
    # middleware (see backend.api.main) has rewritten scope.client; falls
    # back to the raw socket address for direct connections (dev / tests).
    ip = request.client.host if request.client else "unknown"

    # Validate redirect URIs — strict loopback only. We do this before
    # the rate-limit check so a bot probing the surface with garbage
    # input doesn't get to fill its bucket with cheap 422s.
    for u in payload.redirect_uris:
        if not _is_loopback_redirect_uri(u):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(f"redirect_uri must be http://127.0.0.1 or http://localhost (got {u})"),
            )
    # Scope subset check — accept BOTH ``allowed_scopes`` (BSVibe-native
    # list) and RFC 7591 ``scope`` (space-separated string). Latter wins
    # if both present.
    scope_list: list[str] | None = payload.allowed_scopes
    if payload.scope is not None:
        scope_list = [s for s in payload.scope.split() if s]
    scopes = scope_list or [DEFAULT_SCOPE]
    for s in scopes:
        if s not in ALLOWED_SCOPES:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"unknown scope: {s}",
            )
    # RFC 7591 §2 capability checks — fail loudly if the client asks for
    # something we don't support (better than silently registering a
    # client that won't be able to use the AS).
    if payload.grant_types is not None:
        for g in payload.grant_types:
            if g not in ("authorization_code", "refresh_token"):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"unsupported grant_type: {g}",
                )
    if payload.response_types is not None:
        for r in payload.response_types:
            if r != "code":
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"unsupported response_type: {r}",
                )
    if payload.token_endpoint_auth_method not in (None, "none"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"unsupported token_endpoint_auth_method: "
                f"{payload.token_endpoint_auth_method} (only 'none' = PKCE-public)"
            ),
        )
    # Rate limit AFTER input validation. ``_anon_dcr_rate_check`` mutates
    # the bucket on success only.
    if not _anon_dcr_rate_check(ip):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="anonymous DCR rate limit exceeded — try again later",
        )

    row = await register_client(
        session,
        workspace_id=None,
        created_by_user_id=None,
        client_name=payload.client_name,
        redirect_uris=payload.redirect_uris,
        allowed_scopes=scopes,
    )
    await session.commit()
    logger.info(
        "audit.oauth.client_registered_anonymous",
        ip=ip,
        client_id=row.client_id,
        client_name=row.client_name,
        redirect_uris=list(row.redirect_uris),
        allowed_scopes=list(row.allowed_scopes),
    )
    return AnonymousClientResponse(
        client_id=row.client_id,
        client_name=row.client_name,
        redirect_uris=list(row.redirect_uris),
        allowed_scopes=list(row.allowed_scopes),
        created_at=row.created_at,
    )


# ---------------------------------------------------------------------------
# Founder-managed clients (authenticated)
# ---------------------------------------------------------------------------


@v1_router.post("/clients", response_model=ClientResponse, status_code=status.HTTP_201_CREATED)
async def create_client(
    payload: ClientCreateRequest,
    user_row: Annotated[UserRow, Depends(get_current_user_row)],
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> ClientResponse:
    """RFC 7591 §3 — register a new public OAuth client.

    v1 requires founder auth (NOT pure RFC 7591 because anonymous DCR
    would let any visitor pollute the client table). Returns the fresh
    ``client_id`` for the founder to paste into the MCP client config.
    """
    # Validate redirect URIs upfront — block scheme/host that we won't
    # match at /authorize anyway so the founder sees the bad input here.
    for u in payload.redirect_uris:
        try:
            parts = urlsplit(u)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"invalid redirect_uri: {u}",
            ) from exc
        if parts.scheme == "http" and parts.hostname not in (
            "127.0.0.1",
            "localhost",
            "[::1]",
            "::1",
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(f"redirect_uri must be https:// or http://127.0.0.1 (got {u})"),
            )
        if parts.scheme not in ("http", "https"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"redirect_uri scheme must be http(s) (got {u})",
            )
    scopes = payload.allowed_scopes or [DEFAULT_SCOPE]
    for s in scopes:
        if s not in ALLOWED_SCOPES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"unknown scope: {s}",
            )
    row = await register_client(
        session,
        workspace_id=workspace_id,
        created_by_user_id=user_row.id,
        client_name=payload.client_name,
        redirect_uris=payload.redirect_uris,
        allowed_scopes=scopes,
    )
    await session.commit()
    return _client_to_response(row)


@v1_router.get("/clients", response_model=list[ClientResponse])
async def list_clients(
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> list[ClientResponse]:
    rows = await list_clients_for_workspace(session, workspace_id)
    return [_client_to_response(r) for r in rows]


@v1_router.delete("/clients/{client_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_client(
    client_id: str,
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> None:
    row = await revoke_client(session, client_id=client_id, workspace_id=workspace_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    await session.commit()


# ---------------------------------------------------------------------------
# .well-known metadata
# ---------------------------------------------------------------------------


@metadata_router.get("/.well-known/oauth-authorization-server")
async def oauth_authorization_server_metadata() -> dict[str, Any]:
    """RFC 8414 — authorization-server metadata document."""
    settings = get_settings()
    issuer = settings.oauth_issuer
    return {
        "issuer": issuer,
        "authorization_endpoint": f"{issuer}/api/oauth/authorize",
        "token_endpoint": f"{issuer}/api/oauth/token",
        "introspection_endpoint": f"{issuer}/api/oauth/introspect",
        "revocation_endpoint": f"{issuer}/api/oauth/revoke",
        # RFC 8414 — registration_endpoint advertises the OPEN
        # (unauthenticated) DCR surface so MCP clients discover the
        # right URL during ``claude mcp add``. The founder-authed
        # ``/api/v1/oauth/clients`` route is for the Settings UI only
        # and is intentionally NOT in this metadata document.
        "registration_endpoint": f"{issuer}/api/oauth/register",
        "jwks_uri": f"{issuer}/.well-known/jwks.json",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": list(ALLOWED_SCOPES),
        "service_documentation": "https://bsvibe.dev/docs/mcp",
    }


@metadata_router.get("/.well-known/oauth-protected-resource")
async def oauth_protected_resource_metadata() -> dict[str, Any]:
    """RFC 9728 — resource-server metadata for the embedded MCP (D2).

    The embedded MCP server is mounted at ``/mcp`` (NOT ``/api/mcp``) —
    MCP convention is a top-level path so clients construct a clean
    server URL. Claude Code follows the ``WWW-Authenticate`` 401 header
    here to discover the authorization server.
    """
    settings = get_settings()
    issuer = settings.oauth_issuer
    return {
        "resource": f"{issuer.rstrip('/')}/mcp",
        "authorization_servers": [issuer],
        "bearer_methods_supported": ["header"],
        "resource_documentation": "https://bsvibe.dev/docs/mcp",
        "scopes_supported": list(ALLOWED_SCOPES),
    }


@metadata_router.get("/.well-known/jwks.json")
async def jwks() -> dict[str, list[dict[str, Any]]]:
    """RFC 7517 — JSON Web Key Set for ES256 access tokens."""
    return jwks_payload()


__all__ = [
    "ACCESS_TOKEN_AUDIENCE",
    "ALLOWED_SCOPES",
    "DEFAULT_SCOPE",
    "PublicClientResponse",
    "metadata_router",
    "public_router",
    "v1_router",
]
