"""HTTP-surface tests for ``GET /api/v1/events/stream`` (B16 SSE live UX).

Exercises the endpoint over an httpx ASGITransport. Auth is the
query-param-token fallback (browser ``EventSource`` cannot send
``Authorization`` headers — see ``eventsource-sse-auth-trap``); the same JWT
verifier the bearer-header path uses runs here, so an invalid / missing
token still 401s.

Workspace isolation is exercised by publishing into workspace A and
asserting a workspace B subscriber does NOT receive the event.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import jwt
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.api.deps import get_db_session
from backend.api.main import create_app
from backend.api.v1.events import sse_event_stream
from backend.api.v1.live_events import (
    LiveEvent,
    LiveEventBus,
    reset_live_event_bus_for_testing,
)
from backend.identity.db import MembershipRow, UserRow
from backend.identity.workspaces_db import WorkspaceRow
from backend.shared.authz.settings import get_settings as get_auth_settings

from .._support import db_engine

pytestmark = pytest.mark.asyncio

_HS256_SECRET = "test-secret-events-stream-hs256-do-not-use-in-prod-32"


@pytest.fixture(autouse=True)
def _hs256_env(monkeypatch: pytest.MonkeyPatch):
    """Configure HS256 user JWT verification so tests can mint tokens.

    Authz Settings env vars are UN-prefixed (``USER_JWT_*``); the default
    audience is ``bsvibe`` so the minted tokens claim that too.
    """
    monkeypatch.setenv("USER_JWT_ALGORITHM", "HS256")
    monkeypatch.setenv("USER_JWT_SECRET", _HS256_SECRET)
    monkeypatch.setenv("USER_JWT_AUDIENCE", "bsvibe")
    monkeypatch.delenv("USER_JWT_ISSUER", raising=False)
    monkeypatch.delenv("USER_JWT_JWKS_URL", raising=False)
    monkeypatch.delenv("USER_JWT_PUBLIC_KEY", raising=False)
    get_auth_settings.cache_clear()
    reset_live_event_bus_for_testing()
    yield
    get_auth_settings.cache_clear()
    reset_live_event_bus_for_testing()


def _make_token(sub: str) -> str:
    """Mint a minimal HS256 user JWT the endpoint will accept."""
    now = datetime.now(UTC)
    payload = {
        "sub": sub,
        "aud": "bsvibe",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=5)).timestamp()),
    }
    return jwt.encode(payload, _HS256_SECRET, algorithm="HS256")


@pytest_asyncio.fixture
async def db():
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


async def _seed_member(db, workspace_id: uuid.UUID, sub: str, role: str = "owner") -> None:
    async with db() as s:
        s.add(WorkspaceRow(id=workspace_id, name="ws", region="us-1", safe_mode=True))
        user_id = uuid.uuid4()
        s.add(UserRow(id=user_id, supabase_user_id=sub, email="m@example.com"))
        await s.flush()
        s.add(
            MembershipRow(
                id=uuid.uuid4(),
                user_id=user_id,
                workspace_id=workspace_id,
                role=role,
            )
        )
        await s.commit()


def _client(app, db) -> httpx.AsyncClient:
    async def _session():
        async with db() as s:
            yield s

    app.dependency_overrides[get_db_session] = _session
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
async def test_stream_missing_token_rejected(db) -> None:
    """No ``?token=`` query param at all — FastAPI's required-query handling
    returns 422 before the handler runs."""
    app = create_app()
    async with _client(app, db) as c:
        r = await c.get("/api/v1/events/stream")
    assert r.status_code == 422  # missing required query param


async def test_stream_invalid_token_rejected(db) -> None:
    """Garbage token — verified against the same JWT key the bearer header
    uses, so it must 401."""
    app = create_app()
    async with _client(app, db) as c:
        r = await c.get("/api/v1/events/stream?token=not-a-real-jwt")
    assert r.status_code == 401


async def test_stream_valid_token_no_membership_403(db) -> None:
    """A well-formed token whose subject has no workspace membership 403s —
    parallels :func:`get_workspace_id` for the bearer-header endpoints."""
    app = create_app()
    token = _make_token("nobody-sub")
    async with _client(app, db) as c:
        r = await c.get(f"/api/v1/events/stream?token={token}")
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# Streaming wire format — driven through the SSE generator directly (httpx
# ASGITransport buffers streamed bytes until the response closes, which
# would deadlock against the indefinite SSE stream).
# ---------------------------------------------------------------------------
def _parse_frames(byte_chunks: list[bytes]) -> list[dict]:
    """Parse a list of SSE wire chunks into a list of frames.

    Heartbeat comments (``: ping``) become ``{"comment": True}``; real
    events become ``{"event": "...", "data": {...}}``.
    """
    buf = b"".join(byte_chunks)
    frames: list[dict] = []
    for raw in buf.split(b"\n\n"):
        text = raw.decode("utf-8").strip("\n")
        if not text:
            continue
        lines = text.split("\n")
        if all(line.startswith(":") for line in lines):
            frames.append({"comment": True})
            continue
        event_name: str | None = None
        data_payload: dict | None = None
        for line in lines:
            if line.startswith("event: "):
                event_name = line[len("event: ") :]
            elif line.startswith("data: "):
                data_payload = json.loads(line[len("data: ") :])
        frames.append({"event": event_name, "data": data_payload})
    return frames


async def test_sse_stream_emits_initial_heartbeat() -> None:
    """The generator yields a heartbeat comment FIRST so the EventSource
    ``onopen`` fires immediately on connect — without it the consumer hangs
    silent until the first real event lands."""
    bus = LiveEventBus()
    workspace_id = uuid.uuid4()
    gen = sse_event_stream(bus, workspace_id)
    first = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
    assert first == b": ping\n\n"
    await gen.aclose()


async def test_sse_stream_emits_decision_pending_event() -> None:
    """Publishing a ``decision.pending`` event is forwarded as the
    canonical SSE frame ``event: decision.pending\\ndata: {...}\\n\\n``."""
    bus = LiveEventBus()
    workspace_id = uuid.uuid4()
    gen = sse_event_stream(bus, workspace_id)
    # Consume the initial heartbeat.
    await asyncio.wait_for(gen.__anext__(), timeout=1.0)

    # Publish while the generator is parked on queue.get(). Give it one event
    # loop tick to (a) register the subscriber, (b) await the queue.
    await asyncio.sleep(0.01)
    await bus.publish(
        workspace_id,
        LiveEvent(
            event_type="decision.pending",
            data={"decision_id": "abc", "run_id": "r1"},
        ),
    )
    chunk = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
    frames = _parse_frames([chunk])
    assert frames == [{"event": "decision.pending", "data": {"decision_id": "abc", "run_id": "r1"}}]
    await gen.aclose()


async def test_sse_stream_workspace_isolation() -> None:
    """Publishing on workspace A while a generator listens on workspace B
    does NOT cross the boundary — the B-listener only sees the B-publish."""
    bus = LiveEventBus()
    ws_a = uuid.uuid4()
    ws_b = uuid.uuid4()
    gen = sse_event_stream(bus, ws_b)
    # Drop the initial heartbeat.
    await asyncio.wait_for(gen.__anext__(), timeout=1.0)
    await asyncio.sleep(0.01)

    await bus.publish(
        ws_a,
        LiveEvent(event_type="decision.pending", data={"decision_id": "from-a"}),
    )
    await asyncio.sleep(0.05)
    await bus.publish(
        ws_b,
        LiveEvent(event_type="run.terminal", data={"run_id": "from-b"}),
    )
    chunk = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
    frames = _parse_frames([chunk])
    assert frames == [{"event": "run.terminal", "data": {"run_id": "from-b"}}]
    await gen.aclose()


async def test_sse_stream_emits_periodic_heartbeats_when_idle() -> None:
    """When no events arrive within the configured idle window the generator
    yields a heartbeat comment line so proxies don't drop the connection."""
    bus = LiveEventBus()
    workspace_id = uuid.uuid4()
    gen = sse_event_stream(bus, workspace_id, heartbeat_interval_seconds=0.05)
    # Initial heartbeat
    first = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
    assert first == b": ping\n\n"
    # Next yield, with no publish, must be another heartbeat (not an event).
    second = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
    assert second == b": ping\n\n"
    await gen.aclose()


# ---------------------------------------------------------------------------
# HTTP endpoint smoke (connection opens with proxy-safe headers)
# ---------------------------------------------------------------------------
async def test_stream_endpoint_returns_event_stream_headers(db) -> None:
    """The HTTP endpoint serves ``text/event-stream`` + proxy-safe headers.

    Without ``Cache-Control: no-cache`` + ``X-Accel-Buffering: no`` nginx
    (and several CDN proxies) buffer the stream into one shaped chunk and
    the live UX is silently broken in prod.

    Drives the ASGI app directly (not through httpx, which deadlocks against
    an indefinite stream) to read the response prologue (status + headers)
    before tearing the stream down.
    """
    workspace_id = uuid.uuid4()
    sub = "owner-sub-headers"
    await _seed_member(db, workspace_id, sub)
    token = _make_token(sub)

    app = create_app()

    # Wire the same db session override _client(...) uses.
    async def _session():
        async with db() as s:
            yield s

    app.dependency_overrides[get_db_session] = _session

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/api/v1/events/stream",
        "raw_path": b"/api/v1/events/stream",
        "query_string": f"token={token}".encode(),
        "headers": [],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
    }

    received: dict = {}
    received_body_first_chunk = asyncio.Event()

    async def receive() -> dict:
        # The handler doesn't read a body — block forever (until disconnect).
        await asyncio.Event().wait()
        return {}  # pragma: no cover

    async def send(message: dict) -> None:
        if message["type"] == "http.response.start":
            received["status"] = message["status"]
            received["headers"] = {k.decode().lower(): v.decode() for k, v in message["headers"]}
        elif message["type"] == "http.response.body":
            received.setdefault("body", b"")
            received["body"] += message.get("body", b"")
            received_body_first_chunk.set()

    # Drive the ASGI app and wait for the prologue + first body chunk
    # (the initial heartbeat ``: ping``), then tear down.
    task = asyncio.create_task(app(scope, receive, send))
    try:
        await asyncio.wait_for(received_body_first_chunk.wait(), timeout=2.0)
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    assert received["status"] == 200
    assert received["headers"]["content-type"].startswith("text/event-stream")
    assert received["headers"]["cache-control"] == "no-cache"
    assert received["headers"]["x-accel-buffering"] == "no"
    # The first body chunk should be the proxy-keepalive heartbeat — proves
    # the stream actually flushes bytes (no buffer-everything-then-close).
    assert received["body"].startswith(b":")
