"""FastAPI application factory.

Entrypoint:
    uvicorn backend.api.main:create_app --factory --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import redis.asyncio as redis_aio
import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.api.auth import router as auth_router
from backend.api.health import router as health_router
from backend.api.middleware import WorkspaceContextMiddleware
from backend.api.v1 import router as v1_router
from backend.api.v1.events import public_router as events_public_router
from backend.api.v1.live_events import set_live_event_bus_redis
from backend.api.v1.workers import public_router as workers_public_router
from backend.api.webhooks import router as webhooks_router
from backend.config import get_settings
from backend.shared.core.logging import configure_logging

logger = structlog.get_logger(__name__)


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(level="INFO", service_name="bsvibe-app")

    app = FastAPI(
        title="BSVibe",
        version=settings.version,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )
    # Brackets each request so the workspace contextvar (defense layer 1)
    # starts unset and is reset afterwards — no scope leaks across requests.
    app.add_middleware(WorkspaceContextMiddleware)
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
    # SSE live-events stream (B16) — query-param token auth because the
    # browser EventSource cannot send Authorization headers
    # (eventsource-sse-auth-trap). Mounted OUTSIDE the auth-gated v1 router
    # for the same reason, like webhooks + worker register/heartbeat.
    app.include_router(events_public_router, prefix="/api/v1")
    app.include_router(v1_router, prefix="/api")

    # C2 — bind the LiveEventBus singleton to the configured Redis transport
    # so SSE subscribers in THIS container see publishes from the worker
    # container (the audit emit fires there). No Redis URL set → in-memory
    # fallback, useful for dev / tests. Construction is connection-lazy
    # (``redis.asyncio.from_url`` does not connect until the first command),
    # so this never blocks app start; an outage surfaces only at publish /
    # subscribe time and is soft-failed inside the bus.
    if settings.redis_url:
        try:
            redis_client = redis_aio.from_url(settings.redis_url, decode_responses=True)
            set_live_event_bus_redis(redis_client)
            logger.info("live_event_bus_redis_bound", redis_url=settings.redis_url)
        except Exception:  # noqa: BLE001 — never let SSE wiring break app startup
            logger.warning("live_event_bus_redis_bind_failed", exc_info=True)

    return app
