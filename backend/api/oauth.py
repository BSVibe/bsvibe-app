"""/api/oauth/* — RFC 6749 + 7636 (PKCE) + 7591 (DCR) + 7662 + 7009 + 8414.

Public (unauthenticated) endpoints under ``/api/oauth/`` + the two
``/.well-known/`` metadata documents — mounted in
:mod:`backend.api.main` directly, NOT under the JWT-gated v1 router,
because the OAuth surface IS the authentication surface.

The ``/authorize`` endpoint requires the founder's Supabase session via
the existing :func:`backend.shared.authz.deps.get_current_user`
dependency — this is the "user must be logged in to authorize a client"
gate. ``/token`` is unauthenticated by design (clients send PKCE).

Two auth-gated endpoints live here too — they manage the founder's own
OAuth clients (RFC 7591 §3 lets the AS gate DCR; we require founder
auth). They use the ``v1_router`` exposed below, which is mounted under
``/api/v1/oauth`` from :mod:`backend.api.main`.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
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


def _redirect_with_error(
    redirect_uri: str, state: str | None, error: str, description: str
) -> RedirectResponse:
    params = {"error": error, "error_description": description}
    if state:
        params["state"] = state
    sep = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(
        f"{redirect_uri}{sep}{urlencode(params)}",
        status_code=302,
    )


async def _authorize_impl(  # noqa: PLR0911, PLR0912 — OAuth state machine
    request: Request,
    user_row: UserRow,
    workspace_id: uuid.UUID,
    session: AsyncSession,
) -> Any:
    """RFC 6749 §3.1 authorization endpoint.

    GET renders the consent screen; POST commits the consent and 302's
    back to the client's ``redirect_uri`` with a single-use code.
    """
    del user_row  # bootstrap-side-effects only
    if request.method == "POST":
        form = await request.form()
        params = {k: v for k, v in form.multi_items() if isinstance(v, str)}
    else:
        params = dict(request.query_params)

    response_type = params.get("response_type")
    client_id = params.get("client_id")
    redirect_uri = params.get("redirect_uri")
    scope = params.get("scope")
    state = params.get("state")
    code_challenge = params.get("code_challenge")
    code_challenge_method = params.get("code_challenge_method")
    action = params.get("action")  # POST only — "approve" or "deny"

    if response_type != "code":
        return _consent_error(
            "unsupported_response_type",
            "response_type must be 'code'",
        )
    if not client_id:
        return _consent_error("invalid_request", "client_id is required")
    client = await lookup_client_by_client_id(session, client_id)
    if client is None or client.revoked_at is not None:
        return _consent_error("invalid_client", "unknown or revoked client")
    if not redirect_uri:
        return _consent_error("invalid_request", "redirect_uri is required")
    if not match_redirect_uri(list(client.redirect_uris), redirect_uri):
        return _consent_error("invalid_request", "redirect_uri is not registered for this client")
    # Past this point we can safely 302 errors back to the client.
    if not code_challenge:
        return _redirect_with_error(
            redirect_uri, state, "invalid_request", "code_challenge is required"
        )
    if code_challenge_method != "S256":
        return _redirect_with_error(
            redirect_uri,
            state,
            "invalid_request",
            "code_challenge_method must be 'S256'",
        )
    parsed_scope, scope_err = _parse_scope(scope, list(client.allowed_scopes))
    if scope_err is not None:
        return _redirect_with_error(redirect_uri, state, "invalid_scope", scope_err)

    # GET → render consent. POST without "approve"/"deny" → also render
    # (defensive; the form always sends action).
    if request.method == "GET" or action not in ("approve", "deny"):
        return HTMLResponse(_render_consent(client, parsed_scope, params))

    if action == "deny":
        return _redirect_with_error(redirect_uri, state, "access_denied", "user denied the request")

    # Approve — mint code + 302 to client.
    code = await issue_authorization_code(
        session,
        client_id=client.client_id,
        user_id=client.created_by_user_id,
        workspace_id=workspace_id,
        scope=parsed_scope,
        redirect_uri=redirect_uri,
        code_challenge=code_challenge,
    )
    await session.commit()
    sep = "&" if "?" in redirect_uri else "?"
    bounce = {"code": code}
    if state:
        bounce["state"] = state
    return RedirectResponse(f"{redirect_uri}{sep}{urlencode(bounce)}", status_code=302)


@public_router.get("/authorize", operation_id="oauth_authorize_get")
async def authorize_get(
    request: Request,
    user: CurrentUser,
    user_row: Annotated[UserRow, Depends(get_current_user_row)],
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> Any:
    del user
    return await _authorize_impl(request, user_row, workspace_id, session)


@public_router.post("/authorize", operation_id="oauth_authorize_post")
async def authorize_post(
    request: Request,
    user: CurrentUser,
    user_row: Annotated[UserRow, Depends(get_current_user_row)],
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> Any:
    del user
    return await _authorize_impl(request, user_row, workspace_id, session)


def _consent_error(error: str, description: str) -> HTMLResponse:
    """Render an HTML error when redirect_uri isn't known-good yet."""
    html = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<title>OAuth error</title>"
        "<style>body{font-family:system-ui,sans-serif;max-width:520px;"
        "margin:48px auto;padding:0 16px}.code{font-family:ui-monospace,"
        "Menlo,monospace;background:#f3f3f3;padding:.25rem .5rem;"
        "border-radius:.25rem}</style></head><body>"
        "<h1>OAuth error</h1>"
        f"<p>error: <span class='code'>{_escape(error)}</span></p>"
        f"<p>{_escape(description)}</p>"
        "</body></html>"
    )
    return HTMLResponse(html, status_code=400)


def _escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


def _render_consent(client: OAuthClientRow, scope: list[str], params: dict[str, str]) -> str:
    """Server-rendered consent HTML.

    The POST form re-submits every preserved param so the same handler
    can re-validate + commit.
    """
    preserved = {
        k: params[k]
        for k in (
            "response_type",
            "client_id",
            "redirect_uri",
            "scope",
            "state",
            "code_challenge",
            "code_challenge_method",
        )
        if params.get(k)
    }
    hidden = "\n".join(
        f'<input type="hidden" name="{_escape(k)}" value="{_escape(v)}">'
        for k, v in preserved.items()
    )
    scope_items = "".join(f"<li><code>{_escape(s)}</code></li>" for s in scope)
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        f"<title>Authorize {_escape(client.client_name)}</title>"
        "<style>body{font-family:system-ui,sans-serif;max-width:520px;"
        "margin:48px auto;padding:0 16px;color:#111}h1{font-size:1.4rem}"
        ".client{font-family:ui-monospace,Menlo,monospace;background:#f3f3f3;"
        "padding:.25rem .5rem;border-radius:.25rem}ul{padding-left:1.25rem}"
        ".actions{margin-top:1.5rem;display:flex;gap:.75rem}"
        "button{padding:.5rem 1rem;border-radius:.375rem;border:1px solid "
        "#cdcdcd;background:#fff;cursor:pointer;font-size:1rem}"
        "button.approve{background:#0070f3;color:#fff;border-color:#0070f3}"
        "</style></head><body>"
        f"<h1>Authorize <span class='client'>{_escape(client.client_name)}</span></h1>"
        "<p>This application is requesting access to your bsvibe-app account.</p>"
        "<dl><dt><strong>Client ID</strong></dt>"
        f"<dd><code>{_escape(client.client_id)}</code></dd>"
        f"<dt><strong>Scopes</strong></dt><dd><ul>{scope_items}</ul></dd></dl>"
        "<form method='POST' action='/api/oauth/authorize'>"
        f"{hidden}"
        "<div class='actions'>"
        "<button type='submit' name='action' value='deny'>Deny</button>"
        "<button type='submit' name='action' value='approve' class='approve'>"
        "Approve</button></div></form></body></html>"
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
        outcome, pair = await rotate_refresh_token(
            session,
            refresh_token=refresh_token,
            client_id=client_id,
            issuer=issuer,
        )
        if outcome is not RefreshRotateOutcome.ROTATED or pair is None:
            return _OAuthError(401, "invalid_grant", outcome.value)
        await session.commit()
        return TokenResponse(
            access_token=pair.access_token,
            expires_in=pair.expires_in,
            refresh_token=pair.refresh_token,
            scope=" ".join(pair.scope),
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
        "registration_endpoint": f"{issuer}/api/v1/oauth/clients",
        "jwks_uri": f"{issuer}/api/.well-known/jwks.json",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": list(ALLOWED_SCOPES),
        "service_documentation": "https://bsvibe.dev/docs/mcp",
    }


@metadata_router.get("/.well-known/oauth-protected-resource")
async def oauth_protected_resource_metadata() -> dict[str, Any]:
    """RFC 9728 — resource-server metadata for the embedded MCP (D2)."""
    settings = get_settings()
    issuer = settings.oauth_issuer
    return {
        "resource": f"{issuer}/api/mcp",
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
    "metadata_router",
    "public_router",
    "v1_router",
]
