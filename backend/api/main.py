"""FastAPI application factory.

Entrypoint:
    uvicorn backend.api.main:create_app --factory --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import AsyncIterator
from typing import Any

import redis.asyncio as redis_aio
import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.responses import RedirectResponse
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from backend.api.auth import router as auth_router
from backend.api.deps import _get_session_factory
from backend.api.health import router as health_router
from backend.api.middleware import WorkspaceContextMiddleware
from backend.api.oauth import metadata_router as oauth_metadata_router
from backend.api.oauth import public_router as oauth_public_router
from backend.api.v1 import router as v1_router
from backend.api.v1.connector_oauth import public_router as connector_oauth_public_router
from backend.api.v1.events import public_router as events_public_router
from backend.api.v1.live_events import set_live_event_bus_redis
from backend.api.v1.workers import public_router as workers_public_router
from backend.api.webhooks import router as webhooks_router
from backend.config import get_settings
from backend.extensions.plugin.bootstrap import discover_webhook_parsers
from backend.mcp.lifespan import mcp_lifespan
from backend.shared.core.logging import configure_logging
from plugin.audit import register_audit_subscriber

logger = structlog.get_logger(__name__)


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(level="INFO", service_name="bsvibe-app")
    register_audit_subscriber()
    # Lift Q3 / R2c — populate the process-wide WebhookParserRegistry so the
    # public webhook ingress (``/api/webhooks/{connector}/{token}``) can
    # dispatch to each plugin's ``@webhook(...)``-decorated parser. Scans
    # ``plugin/<name>/webhook.py`` at app startup; soft-fails per plugin so
    # one missing module never blocks the API. Idempotent (every connector
    # the loader sees re-registers into the same singleton).
    discover_webhook_parsers()

    session_factory = _get_session_factory()

    @contextlib.asynccontextmanager
    async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with mcp_lifespan(app, session_factory=session_factory):
            yield

    app = FastAPI(
        title="BSVibe",
        version=settings.version,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        lifespan=_lifespan,
        # Lift D2 followup — `redirect_slashes=True` (FastAPI default)
        # turned `POST /mcp` into a 307 redirect to `/mcp/` with a
        # `Location: http://...` URL generated from `scope.scheme` —
        # which is `http` behind Cloudflare since the inner uvicorn
        # never sees TLS. That brittle HTTPS-downgrade-then-CF-upgrade
        # dance breaks MCP clients that don't follow 307. We register
        # `/mcp` AND `/mcp/` as the same transport below, so the
        # redirect is unnecessary in the first place.
        redirect_slashes=False,
    )
    # Brackets each request so the workspace contextvar (defense layer 1)
    # starts unset and is reset afterwards — no scope leaks across requests.
    app.add_middleware(WorkspaceContextMiddleware)
    # Lift D2 followup — honor X-Forwarded-Proto so URL builders + redirects
    # emit `https://` behind Cloudflare instead of the inner `http://`.
    # Trust any upstream (`*`) because Cloudflare terminates TLS and the
    # backend container only ever sees the CF→origin hop.
    app.add_middleware(ProxyHeadersMiddleware, trusted_hosts="*")
    # CORS for the browser PWA calling the backend cross-origin (Bearer-header
    # auth, NOT cookies → allow_credentials=False; explicit allow-list, never
    # "*"). Added LAST so it is OUTERMOST: in Starlette the middleware added
    # last runs first, so CORSMiddleware handles the preflight OPTIONS and
    # stamps the ACAO header before WorkspaceContextMiddleware / routing run.
    # allow_headers covers the custom request headers the BROWSER sends:
    # X-BSVibe-Account-Id (billing account, backend.api.deps) and
    # X-Active-Tenant (Tier 3.2 — raw Supabase JWT carries no tenant claim).
    # X-Idempotency-Key is server-to-server (webhook ingress), never browser.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allowed_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-BSVibe-Account-Id", "X-Active-Tenant"],
    )
    app.include_router(health_router, prefix="/api")
    app.include_router(auth_router, prefix="/api")
    # Connector webhook ingress is PUBLIC (external callback) — mounted under
    # /api directly, NOT under the auth-gated v1 router (Workflow §11.2).
    app.include_router(webhooks_router, prefix="/api")
    # Executor-worker register/heartbeat are install-token / worker-token authed
    # (a headless worker has no Supabase session JWT) — mounted under /api/v1
    # directly, NOT under the auth-gated v1 router, like the webhooks ingress.
    app.include_router(workers_public_router, prefix="/api/v1")
    # Embedded OAuth 2.0 authorization server (Lift D1).
    # ``/api/oauth/{authorize,token,introspect,revoke}`` are the public
    # OAuth surface (mounted under /api, NOT v1, since the OAuth flow is
    # itself the authentication surface). ``/api/v1/oauth/clients`` is
    # the founder-facing DCR management UI surface — Supabase-JWT
    # authed via the v1 router.
    app.include_router(oauth_public_router, prefix="/api")
    # RFC 8414 §3 — the AS metadata document MUST be served at
    # ``<issuer>/.well-known/oauth-authorization-server``. Our issuer is
    # ``https://api.bsvibe.dev`` (no ``/api`` suffix), so the well-known
    # routes mount at root, NOT under ``/api``. Mounting under ``/api``
    # makes Claude Code / other strict MCP clients 404 during OAuth
    # discovery — the SDK chokes on FastAPI's ``{"detail":"Not Found"}``
    # body that lacks the OAuth ``error`` field.
    app.include_router(oauth_metadata_router)
    # SSE live-events stream (B16) — query-param token auth because the
    # browser EventSource cannot send Authorization headers
    # (eventsource-sse-auth-trap). Mounted OUTSIDE the auth-gated v1 router
    # for the same reason, like webhooks + worker register/heartbeat.
    app.include_router(events_public_router, prefix="/api/v1")
    # Connector OAuth callback — public (the third party's browser redirect has
    # no bsvibe session), mounted outside the auth-gated v1 router like the
    # other public callbacks.
    app.include_router(connector_oauth_public_router, prefix="/api/v1")
    app.include_router(v1_router, prefix="/api")

    # Embedded MCP server (Lift D2) — mounted at /mcp (NOT under /api — MCP
    # convention is a top-level path so clients construct a clean server
    # URL). The Streamable HTTP transport authenticates the Bearer token
    # against the embedded OAuth server's JWKS (Lift D1) and verifies the
    # ``jti`` against ``OAuthAccessTokenRow.revoked_at`` per request.
    # 401s carry the RFC 6750 + RFC 9728 ``WWW-Authenticate`` header so
    # Claude Code discovers the authorization server via the resource
    # metadata document. The ASGI app is built inside the lifespan and
    # delegated to here through ``app.state.mcp_asgi``.
    async def _mcp_entrypoint(scope: Any, receive: Any, send: Any) -> None:
        asgi = getattr(app.state, "mcp_asgi", None)
        if asgi is None:  # pragma: no cover — lifespan must run first in prod
            await send(
                {
                    "type": "http.response.start",
                    "status": 503,
                    "headers": [(b"content-type", b"application/json")],
                }
            )
            await send(
                {
                    "type": "http.response.body",
                    "body": b'{"error":"mcp_lifespan_not_ready"}',
                }
            )
            return
        await asgi(scope, receive, send)

    app.mount("/mcp", _mcp_entrypoint)

    # Starlette ``app.mount("/mcp", ...)`` matches the trailing-slash form
    # (``/mcp/``) but NOT a request to the bare ``/mcp`` — it 404s.
    # FastAPI ``redirect_slashes=False`` (Lift D2 followup) means no auto
    # redirect from ``/mcp`` to ``/mcp/`` either. Add an explicit redirect
    # so MCP clients that construct the no-slash URL (Claude Code, manual
    # curl smoke-tests) still reach the transport. 307 preserves both the
    # request body and the HTTP method, and `ProxyHeadersMiddleware` makes
    # the `Location` header an ``https://`` URL.
    @app.api_route("/mcp", methods=["GET", "POST", "HEAD"], include_in_schema=False)
    async def _mcp_no_slash_redirect(request: Request) -> RedirectResponse:
        query = request.url.query
        target = "/mcp/" + (f"?{query}" if query else "")
        return RedirectResponse(url=target, status_code=307)

    # C2 — bind the LiveEventBus singleton to the configured Redis transport
    # so SSE subscribers in THIS container see publishes from the worker
    # container (the audit emit fires there). No Redis URL set → in-memory
    # fallback, useful for dev / tests. Construction is connection-lazy
    # (``redis.asyncio.from_url`` does not connect until the first command),
    # so this never blocks app start; an outage surfaces only at publish /
    # subscribe time and is soft-failed inside the bus.
    #
    # Skip under pytest: glue tests instantiate ``create_app()`` per-test on
    # per-test event loops, and binding a real Redis client into the
    # process-wide singleton leaks connection-pool Futures across event
    # loops — surfaced as ``got Future attached to a different loop`` in
    # later non-glue tests when audit emit fires the bridge.
    if settings.redis_url and not os.environ.get("PYTEST_CURRENT_TEST"):
        try:
            redis_client = redis_aio.from_url(settings.redis_url, decode_responses=True)
            set_live_event_bus_redis(redis_client)
            logger.info("live_event_bus_redis_bound", redis_url=settings.redis_url)
        except Exception:  # noqa: BLE001 — never let SSE wiring break app startup
            logger.warning("live_event_bus_redis_bind_failed", exc_info=True)

    return app
