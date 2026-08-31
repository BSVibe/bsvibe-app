"""/api/v1/notifications/prefs — workspace-scoped notification preferences.

The store holds, per workspace, an events x channels enable matrix plus a
quiet-hours window. There is exactly one row per workspace; the GET is
get-or-create (a workspace with no row yet reads the sensible defaults, which
are then persisted). The PUT replaces the matrix + quiet hours wholesale.

Since Notifier N1a the channel COLUMNS are no longer a fixed set — they are
derived per workspace from its connector bindings (``available_channels`` on the
GET response) plus the always-present ``in_app`` inbox. The matrix validator
therefore fixes the EVENT rows but tolerates any channel keys.

These tests deliberately do NOT override the prefs / channel resolution — they
exercise the real get-or-create + real binding resolution against an in-memory
SQLite (or real PG when ``BSVIBE_DATABASE_URL`` is set), mirroring
``test_v1_account.py``.
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
from backend.connectors.db import ConnectorAccountRow
from backend.notifications.db import DEFAULT_EVENTS, NotificationPrefsRow

from .._support import db_engine, fake_current_user

pytestmark = pytest.mark.asyncio

# The seed channel set for a fresh workspace (no connectors) is the inbox only.


def _full_grid(value: bool) -> dict:
    """알려진 이벤트 전부를 덮는 매트릭스 — 이벤트당 스위치 하나."""
    return {event: value for event in DEFAULT_EVENTS}


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

    # Every event row is present; the seed expresses only the in_app inbox.
    assert set(body["matrix"].keys()) == set(DEFAULT_EVENTS)
    for event_id in DEFAULT_EVENTS:
        assert isinstance(body["matrix"][event_id], bool)

    # Sensible defaults: "needs you" lands in the inbox; daily brief is calm.
    assert body["matrix"]["needs_you"] is True
    assert body["matrix"]["daily_brief"] is False

    # No connectors bound → only the inbox is an available channel.
    assert body["available_channels"] == ["in_app"]

    # Quiet hours default off, with a sane window.
    assert body["quiet_hours_enabled"] is False
    assert body["quiet_hours_start"] == "22:00"
    assert body["quiet_hours_end"] == "08:00"


async def test_available_channels_derived_from_telegram_binding(client, db, workspace_id) -> None:
    """[C] Attach ONLY a telegram connector → channels become in_app + telegram.

    This FAILS on pre-N1a code (no ``available_channels`` field; channels frozen
    to the hardcoded 3-col grid that can't express telegram). Real binding
    resolution runs against the same DB — nothing about the channel derivation
    is overridden or pre-seeded beyond the connector row itself.
    """
    async with db() as s:
        s.add(
            ConnectorAccountRow(
                workspace_id=workspace_id,
                connector="telegram",
                webhook_token=uuid.uuid4().hex,
                signing_secret_ciphertext="ciphertext",
                delivery_config={"chat_id": "42"},
                is_active=True,
            )
        )
        await s.commit()

    r = await client.get("/api/v1/notifications/prefs")
    assert r.status_code == 200, r.text
    assert r.json()["available_channels"] == ["in_app", "telegram"]


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
    new_matrix = _full_grid(False)
    new_matrix["shipped"] = True
    payload = {
        "matrix": new_matrix,
        "quiet_hours_enabled": True,
        "quiet_hours_start": "23:30",
        "quiet_hours_end": "07:15",
    }
    put = await client.put("/api/v1/notifications/prefs", json=payload)
    assert put.status_code == 200, put.text
    assert put.json()["matrix"]["shipped"] is True
    assert put.json()["matrix"]["needs_you"] is False
    # PUT response also carries the derived channels.
    assert put.json()["available_channels"] == ["in_app"]

    got = await client.get("/api/v1/notifications/prefs")
    assert got.status_code == 200
    body = got.json()
    assert body["matrix"]["shipped"] is True
    assert body["matrix"]["needs_you"] is False
    assert body["quiet_hours_enabled"] is True
    assert body["quiet_hours_start"] == "23:30"
    assert body["quiet_hours_end"] == "07:15"


async def test_put_rejects_the_old_per_channel_shape(client) -> None:
    """전제가 뒤집혔다 — 채널 축을 접었으므로 중첩 모양은 **거절**한다.

    앞 세대는 *"채널 키는 파생되므로 무엇이든 관용한다"* 였다. 그 관용이 바로
    형님이 겪은 버그의 토대였다: 저장된 키만 배달되므로, 관용된 키 집합이 곧
    조용한 정책이 됐다. 두 형태가 공존하면 send 경로가 중첩 dict 를 truthy 로
    읽어 **끈 이벤트까지 켜진다.**
    """
    matrix = {event: {"in_app": True, "telegram": True} for event in DEFAULT_EVENTS}
    payload = {
        "matrix": matrix,
        "quiet_hours_enabled": False,
        "quiet_hours_start": "22:00",
        "quiet_hours_end": "08:00",
    }
    r = await client.put("/api/v1/notifications/prefs", json=payload)
    assert r.status_code == 422, r.text


async def test_put_is_scoped_to_a_single_row_per_workspace(client, db) -> None:
    """A second PUT updates the same row rather than inserting a new one."""
    await client.get("/api/v1/notifications/prefs")  # seed defaults
    await client.put(
        "/api/v1/notifications/prefs",
        json={
            "matrix": _full_grid(True),
            "quiet_hours_enabled": False,
            "quiet_hours_start": "22:00",
            "quiet_hours_end": "08:00",
        },
    )
    async with db() as s:
        rows = (await s.execute(select(NotificationPrefsRow))).scalars().all()
        assert len(rows) == 1


async def test_put_rejects_unknown_event_key(client) -> None:
    """An unknown event id in the matrix is a 422 (events are still fixed)."""
    payload = {
        "matrix": {"not_an_event": {"in_app": True}},
        "quiet_hours_enabled": False,
        "quiet_hours_start": "22:00",
        "quiet_hours_end": "08:00",
    }
    r = await client.put("/api/v1/notifications/prefs", json=payload)
    assert r.status_code == 422, r.text


async def test_put_rejects_non_bool_channel_value(client) -> None:
    """Channel keys are open, but their values must be booleans."""
    payload = {
        "matrix": {e: {"in_app": ["not", "a", "bool"]} for e in DEFAULT_EVENTS},
        "quiet_hours_enabled": False,
        "quiet_hours_start": "22:00",
        "quiet_hours_end": "08:00",
    }
    r = await client.put("/api/v1/notifications/prefs", json=payload)
    assert r.status_code == 422, r.text


async def test_put_rejects_malformed_quiet_hours_time(client) -> None:
    payload = {
        "matrix": _full_grid(True),
        "quiet_hours_enabled": True,
        "quiet_hours_start": "25:99",
        "quiet_hours_end": "08:00",
    }
    r = await client.put("/api/v1/notifications/prefs", json=payload)
    assert r.status_code == 422, r.text


async def test_put_rejects_extra_field(client) -> None:
    """extra=forbid on the request schema (available_channels is not settable)."""
    payload = {
        "matrix": _full_grid(True),
        "quiet_hours_enabled": False,
        "quiet_hours_start": "22:00",
        "quiet_hours_end": "08:00",
        "available_channels": ["in_app", "telegram"],
    }
    r = await client.put("/api/v1/notifications/prefs", json=payload)
    assert r.status_code == 422, r.text
