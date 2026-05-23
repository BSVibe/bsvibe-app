"""/api/v1/notifications/prefs — workspace-scoped notification preferences.

The store holds, per workspace, an events x channels enable matrix plus a
quiet-hours window. There is exactly one row per workspace; the GET is
get-or-create (a workspace with no row yet reads the sensible defaults, which
are then persisted). The PUT replaces the matrix + quiet hours wholesale.

These tests deliberately do NOT override the prefs resolution — they exercise
the real get-or-create against an in-memory SQLite (or real PG when
``BSVIBE_DATABASE_URL`` is set), mirroring ``test_v1_account.py``.
"""

from __future__ import annotations

import base64
import uuid

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.api.deps import get_current_user, get_db_session, get_workspace_id
from backend.api.main import create_app
from backend.config import get_settings
from backend.notifications.db import (
    DEFAULT_CHANNELS,
    DEFAULT_EVENTS,
    NotificationPrefsRow,
)

from .._support import db_engine, fake_current_user

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def db(monkeypatch):
    monkeypatch.setenv("BSVIBE_GATEWAY_KMS_KEY_B64", base64.urlsafe_b64encode(b"0" * 32).decode())
    get_settings.cache_clear()
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)
    get_settings.cache_clear()


@pytest.fixture
def workspace_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest_asyncio.fixture
async def client(db, workspace_id):
    app = create_app()

    def _ws() -> uuid.UUID:
        return workspace_id

    async def _session():
        async with db() as s:
            yield s

    app.dependency_overrides[get_current_user] = fake_current_user()
    app.dependency_overrides[get_workspace_id] = _ws
    app.dependency_overrides[get_db_session] = _session

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_get_prefs_returns_defaults_when_none_exist(client) -> None:
    """A workspace with no row reads the sensible defaults (get-or-create)."""
    r = await client.get("/api/v1/notifications/prefs")
    assert r.status_code == 200, r.text
    body = r.json()

    # The full events x channels matrix is present.
    assert set(body["matrix"].keys()) == set(DEFAULT_EVENTS)
    for event_id in DEFAULT_EVENTS:
        assert set(body["matrix"][event_id].keys()) == set(DEFAULT_CHANNELS)

    # Sensible defaults: "needs you" is on for every channel.
    assert body["matrix"]["needs_you"] == {"in_app": True, "email": True, "slack": True}
    # Daily brief defaults to email-only.
    assert body["matrix"]["daily_brief"]["email"] is True

    # Quiet hours default off, with a sane window.
    assert body["quiet_hours_enabled"] is False
    assert body["quiet_hours_start"] == "22:00"
    assert body["quiet_hours_end"] == "08:00"


async def test_get_prefs_persists_single_row(client, db) -> None:
    """Two reads return the same row; get-or-create writes exactly one row."""
    r1 = await client.get("/api/v1/notifications/prefs")
    r2 = await client.get("/api/v1/notifications/prefs")
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json() == r2.json()

    async with db() as s:
        rows = (await s.execute(select(NotificationPrefsRow))).scalars().all()
        assert len(rows) == 1


async def test_put_then_get_round_trips_matrix_and_quiet_hours(client, workspace_id) -> None:
    """PUT replaces the matrix + quiet hours; the next GET reflects it."""
    # Flip every cell off, then turn one back on, and set quiet hours.
    new_matrix = {
        event: {channel: False for channel in DEFAULT_CHANNELS} for event in DEFAULT_EVENTS
    }
    new_matrix["shipped"]["email"] = True
    payload = {
        "matrix": new_matrix,
        "quiet_hours_enabled": True,
        "quiet_hours_start": "23:30",
        "quiet_hours_end": "07:15",
    }
    put = await client.put("/api/v1/notifications/prefs", json=payload)
    assert put.status_code == 200, put.text
    assert put.json()["matrix"]["shipped"]["email"] is True
    assert put.json()["matrix"]["needs_you"]["in_app"] is False

    got = await client.get("/api/v1/notifications/prefs")
    assert got.status_code == 200
    body = got.json()
    assert body["matrix"]["shipped"]["email"] is True
    assert body["matrix"]["needs_you"]["in_app"] is False
    assert body["quiet_hours_enabled"] is True
    assert body["quiet_hours_start"] == "23:30"
    assert body["quiet_hours_end"] == "07:15"


async def test_put_is_scoped_to_a_single_row_per_workspace(client, db) -> None:
    """A second PUT updates the same row rather than inserting a new one."""
    await client.get("/api/v1/notifications/prefs")  # seed defaults
    await client.put(
        "/api/v1/notifications/prefs",
        json={
            "matrix": {e: {c: True for c in DEFAULT_CHANNELS} for e in DEFAULT_EVENTS},
            "quiet_hours_enabled": False,
            "quiet_hours_start": "22:00",
            "quiet_hours_end": "08:00",
        },
    )
    async with db() as s:
        rows = (await s.execute(select(NotificationPrefsRow))).scalars().all()
        assert len(rows) == 1


async def test_put_rejects_unknown_event_key(client) -> None:
    """An unknown event id in the matrix is a 422 (the matrix is validated)."""
    payload = {
        "matrix": {"not_an_event": {c: True for c in DEFAULT_CHANNELS}},
        "quiet_hours_enabled": False,
        "quiet_hours_start": "22:00",
        "quiet_hours_end": "08:00",
    }
    r = await client.put("/api/v1/notifications/prefs", json=payload)
    assert r.status_code == 422, r.text


async def test_put_rejects_unknown_channel_key(client) -> None:
    payload = {
        "matrix": {e: {"carrier_pigeon": True} for e in DEFAULT_EVENTS},
        "quiet_hours_enabled": False,
        "quiet_hours_start": "22:00",
        "quiet_hours_end": "08:00",
    }
    r = await client.put("/api/v1/notifications/prefs", json=payload)
    assert r.status_code == 422, r.text


async def test_put_rejects_malformed_quiet_hours_time(client) -> None:
    payload = {
        "matrix": {e: {c: True for c in DEFAULT_CHANNELS} for e in DEFAULT_EVENTS},
        "quiet_hours_enabled": True,
        "quiet_hours_start": "25:99",
        "quiet_hours_end": "08:00",
    }
    r = await client.put("/api/v1/notifications/prefs", json=payload)
    assert r.status_code == 422, r.text


async def test_put_rejects_extra_field(client) -> None:
    """extra=forbid on the request schema."""
    payload = {
        "matrix": {e: {c: True for c in DEFAULT_CHANNELS} for e in DEFAULT_EVENTS},
        "quiet_hours_enabled": False,
        "quiet_hours_start": "22:00",
        "quiet_hours_end": "08:00",
        "surprise": "nope",
    }
    r = await client.put("/api/v1/notifications/prefs", json=payload)
    assert r.status_code == 422, r.text
