"""/api/v1/events — Server-Sent Events live notification channel (B16).

One streaming endpoint, ``GET /api/v1/events/stream``, that pushes the three
highest-signal events to the PWA:

* ``decision.pending`` — a new founder-needs-you item (Decisions inbox +
  Brief "Needs you" lane wake up)
* ``run.terminal`` — a run reached verified / needs_decision / system_error
  (Brief Work-Stream + RunDetail wake up)
* ``delivery.queued`` — a Safe-Mode held delivery appeared (Brief "Needs you"
  wakes up)

Auth quirk (per :doc:`eventsource-sse-auth-trap`): the browser
``EventSource`` API can NOT send custom headers, so this endpoint accepts a
``?token=`` query parameter as a fallback. The token is verified through the
SAME :func:`backend.shared.authz.auth.verify_user_jwt` path the bearer
header uses — no separate trust boundary, no fake auth. The endpoint is
mounted by :mod:`backend.api.main` OUTSIDE the auth-gated v1 aggregate
(like the webhook + worker public routers) so the router-level
``Depends(get_current_user)`` doesn't refuse a header-less request first.

Workspace isolation is structural — the handler resolves the caller's
workspace from their JWT subject + membership and subscribes ONLY to that
workspace on the :class:`LiveEventBus`. A publish on workspace A simply
never enters a workspace B subscriber's queue.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import get_db_session
from backend.api.v1.live_events import (
    LiveEventBus,
    encode_heartbeat,
    encode_sse,
    get_live_event_bus,
)
from backend.identity.service import resolve_workspace_id
from backend.shared.authz.auth import AuthError, parse_user_token, verify_user_jwt
from backend.shared.authz.settings import get_settings as get_auth_settings

# Heartbeat cadence — 25 seconds. Long enough to keep CPU/network noise low,
# short enough that nginx / cloudflare / Vercel idle-close (typically 30-60s)
# does not drop the connection.
HEARTBEAT_INTERVAL_SECONDS = 25.0

# Public router — mounted at /api/v1 in backend.api.main, NOT under the
# auth-gated v1 aggregate (which would reject the header-less browser
# request before our query-param token verification runs).
public_router = APIRouter()


async def sse_event_stream(
    bus: LiveEventBus,
    workspace_id: uuid.UUID,
    *,
    heartbeat_interval_seconds: float = HEARTBEAT_INTERVAL_SECONDS,
) -> AsyncIterator[bytes]:
    """Yield SSE-encoded bytes for a workspace's live event subscription.

    Exposed at module scope (not just an inner closure) so unit tests can
    drive the generator directly without running the whole HTTP stack —
    useful because httpx's ASGITransport doesn't surface streamed bytes
    while the handler is still active.

    Emits an initial heartbeat comment on connect (so the EventSource
    ``onopen`` fires before any real event lands), then forwards each
    published :class:`LiveEvent` as an ``event: <type>\\ndata: <json>\\n\\n``
    frame. Idle gaps longer than ``heartbeat_interval_seconds`` get a
    ``: ping`` comment to keep proxies' idle-close from dropping the
    connection.
    """
    # Register the subscriber BEFORE yielding anything so a publish that
    # races the consumer's first ``__anext__`` is still enqueued (the
    # subscriber must exist on the bus the moment the response starts
    # streaming, not after the consumer pulls the first byte).
    async with bus.subscribe(workspace_id) as queue:
        yield encode_heartbeat()
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=heartbeat_interval_seconds)
            except TimeoutError:
                yield encode_heartbeat()
                continue
            yield encode_sse(event)


@public_router.get("/events/stream")
async def events_stream(
    token: Annotated[str, Query(description="Supabase user JWT (EventSource has no headers)")],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> StreamingResponse:
    """Stream live events for the caller's workspace.

    Verifies the ``?token=`` query param against the same JWT verifier
    Bearer-header auth uses, resolves the caller's active workspace via
    :func:`resolve_workspace_id`, then subscribes to the in-memory
    :class:`LiveEventBus` and streams events as ``text/event-stream`` frames.
    A heartbeat comment (``: ping``) is emitted every
    :data:`HEARTBEAT_INTERVAL_SECONDS` so idle TCP connections survive
    proxies' idle-close.

    401 — token missing / invalid / expired.
    403 — caller has no workspace membership.
    """
    bus = get_live_event_bus()

    # ── auth (query-param token, per eventsource-sse-auth-trap) ──────────
    auth_settings = get_auth_settings()
    try:
        payload = verify_user_jwt(token, auth_settings)
    except AuthError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
    user = parse_user_token(payload)

    workspace_id = await resolve_workspace_id(session, supabase_user_id=user.id)
    if workspace_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="no workspace membership for principal",
        )

    return StreamingResponse(
        sse_event_stream(bus, workspace_id),
        media_type="text/event-stream",
        headers={
            # ``no-cache`` so intermediate caches don't buffer the event
            # stream into a single 4xx-shaped chunk. ``X-Accel-Buffering: no``
            # disables nginx response buffering on the same path.
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


__all__ = [
    "HEARTBEAT_INTERVAL_SECONDS",
    "events_stream",
    "public_router",
    "sse_event_stream",
]
