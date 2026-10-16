"""A worker that can no longer sign in to Claude tells the founder (measured outage 2026-10-15).

The host worker's Claude OAuth refresh token died (``invalid_grant``) at 2026-10-15 14:26Z.
From then on every agent turn failed in one second with no usage, and the keepalive logged
``claude_auth_keepalive_definitive_failure`` once an hour — for ~17 hours, to a log nobody
read. The Monday report run would have failed the same way, silently.

The worker already KNOWS (the keepalive classifies the failure as definitive). This carries
that verdict to the backend on the heartbeat — as the ``X-BSVibe-Worker-Claude-Auth`` HEADER,
never a body field: ``HeartbeatBody`` is ``extra="forbid"``, so a body field would 422 every
heartbeat a newer worker sends an older backend (#965's reason, same here) — and the backend
turns the TRANSITION into one notification through the ordinary outbox:

* ``failed`` after healthy → ``worker_auth_down``, once (a repeat changes nothing)
* ``ok`` after ``failed`` → the all-clear, in words that do not say "down"
* ``ok`` with nothing announced, no header, or a value it does not know → nothing

Driven through the real ``POST /api/v1/workers/heartbeat`` and, for delivery, the real
:class:`NotifyWorker` — the producer's payload must be one the consumer can send.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import backend.executors.db  # noqa: F401
import backend.identity.workspaces_db  # noqa: F401
import backend.notifications.db  # noqa: F401
from backend.api.deps import get_db_session
from backend.api.main import create_app
from backend.data.rls import workspace_session_scope
from backend.executors import service
from backend.executors.db import WorkerRow
from backend.identity.workspaces_db import WorkspaceRow
from backend.notifications.db import NotificationEventRow

from .._support import db_engine

pytestmark = pytest.mark.asyncio

_HEADER = "X-BSVibe-Worker-Claude-Auth"


@pytest_asyncio.fixture
async def sf() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


@pytest_asyncio.fixture
async def client(sf) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app()

    async def _session() -> AsyncIterator[AsyncSession]:
        async with sf() as s:
            yield s

    app.dependency_overrides[get_db_session] = _session
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


async def _worker(sf, *, language: str = "ko") -> tuple[uuid.UUID, uuid.UUID, str]:
    ws, token = uuid.uuid4(), uuid.uuid4().hex
    async with sf() as s, workspace_session_scope(s, ws):
        s.add(WorkspaceRow(id=ws, name="ws", language=language))
        await s.flush()
        worker = WorkerRow(
            workspace_id=ws,
            name="mac-mini-e2e",
            labels=[],
            capabilities=["claude_code"],
            status="online",
            last_heartbeat=datetime.now(UTC),
            token_hash=service._hash_token(token),
            is_active=True,
        )
        s.add(worker)
        await s.commit()
        return ws, worker.id, token


async def _beat(client: httpx.AsyncClient, token: str, auth: str | None) -> httpx.Response:
    headers = {"X-Worker-Token": token}
    if auth is not None:
        headers[_HEADER] = auth
    return await client.post("/api/v1/workers/heartbeat", headers=headers, json={"in_flight": 0})


async def _alerts(sf, ws: uuid.UUID) -> list[NotificationEventRow]:
    async with sf() as s, workspace_session_scope(s, ws):
        rows = (
            await s.execute(
                select(NotificationEventRow).where(NotificationEventRow.event == "worker_auth_down")
            )
        ).scalars()
        return list(rows)


async def _failed_at(sf, ws: uuid.UUID, worker_id: uuid.UUID) -> Any:
    async with sf() as s, workspace_session_scope(s, ws):
        row = await s.get(WorkerRow, worker_id)
        assert row is not None
        return row.claude_auth_failed_at


async def test_a_worker_that_cannot_sign_in_alerts_the_founder_once(sf, client) -> None:
    ws, worker_id, token = await _worker(sf)

    assert (await _beat(client, token, "failed")).status_code == 200
    assert (await _beat(client, token, "failed")).status_code == 200  # the next minute

    (alert,) = await _alerts(sf, ws)
    assert alert.payload["recovered"] is False
    assert "mac-mini-e2e" in alert.payload["body"]
    assert "claude-login" in alert.payload["body"], "the alert must say how to fix it"
    assert alert.payload["title"] == "워커가 Claude 에 로그인하지 못해요"
    assert await _failed_at(sf, ws, worker_id) is not None


async def test_signing_back_in_sends_the_all_clear(sf, client) -> None:
    ws, worker_id, token = await _worker(sf, language="en")
    await _beat(client, token, "failed")

    await _beat(client, token, "ok")
    await _beat(client, token, "ok")

    alerts = await _alerts(sf, ws)
    assert [a.payload["recovered"] for a in alerts] == [False, True]
    recovered = alerts[1]
    assert "can't" not in recovered.payload["title"].lower()
    assert "back" in recovered.payload["title"].lower()
    assert await _failed_at(sf, ws, worker_id) is None


async def test_a_healthy_worker_says_nothing(sf, client) -> None:
    ws, _worker_id, token = await _worker(sf)

    await _beat(client, token, "ok")
    await _beat(client, token, None)

    assert await _alerts(sf, ws) == []


async def test_no_header_leaves_an_announced_failure_alone(sf, client) -> None:
    """An older worker build sends no header — silence is not a recovery."""
    ws, worker_id, token = await _worker(sf)
    await _beat(client, token, "failed")

    await _beat(client, token, None)

    assert [a.payload["recovered"] for a in await _alerts(sf, ws)] == [False]
    assert await _failed_at(sf, ws, worker_id) is not None


async def test_an_unknown_value_is_ignored_not_refused(sf, client) -> None:
    """The heartbeat is the worker's lifeline: a header it cannot read must never 4xx it."""
    ws, _worker_id, token = await _worker(sf)

    r = await _beat(client, token, "maybe")

    assert r.status_code == 200
    assert await _alerts(sf, ws) == []


async def _delivered(sf, client, *, saved_matrix: dict[str, bool] | None) -> list[tuple[str, str]]:
    """Bind telegram, report a failure, drain the real NotifyWorker; what went out."""
    from backend.connectors.db import ConnectorAccountRow
    from backend.notifications.db import NotificationPrefsRow
    from backend.workflow.infrastructure.workers.notify_worker import NotifyWorker

    ws, _worker_id, token = await _worker(sf)
    async with sf() as s, workspace_session_scope(s, ws):
        if saved_matrix is not None:
            s.add(NotificationPrefsRow(workspace_id=ws, matrix=saved_matrix))
        s.add(
            ConnectorAccountRow(
                workspace_id=ws,
                connector="telegram",
                webhook_token=uuid.uuid4().hex,
                signing_secret_ciphertext="ciphertext",
                delivery_config={"chat_id": "42"},
                is_active=True,
            )
        )
        await s.commit()
    await _beat(client, token, "failed")

    sent: list[tuple[str, str]] = []

    class _Sender:
        async def send(self, *, connector, content, delivery_config, signing_secret_ciphertext):  # noqa: ANN001, ANN003, ARG002
            sent.append((connector, content.title))

    await NotifyWorker(session_factory=sf, sender=_Sender()).drain_once()
    return sent


#: A matrix saved before this event existed: every older event explicit, this one ABSENT.
_SAVED_BEFORE = {
    "needs_you": True,
    "review_ready": True,
    "triggered": True,
    "shipped": True,
    "failed": True,
    "daily_brief": False,
    "auth_down": True,
}


async def test_the_alert_reaches_a_bound_channel(sf, client) -> None:
    """Seam: the producer's real payload through the real NotifyWorker on a fresh workspace."""
    assert await _delivered(sf, client, saved_matrix=None) == [
        ("telegram", "워커가 Claude 에 로그인하지 못해요")
    ]


async def test_a_workspace_that_saved_its_settings_before_this_event_still_gets_it(
    sf, client
) -> None:
    """The real case: 형님's prefs row predates this event, so the key is ABSENT — and an outage
    alert nobody could have opted into must still arrive (``DEFAULT_ON_EVENTS``)."""
    assert await _delivered(sf, client, saved_matrix=_SAVED_BEFORE) == [
        ("telegram", "워커가 Claude 에 로그인하지 못해요")
    ]


async def test_an_explicit_off_is_still_respected(sf, client) -> None:
    """Control: default-on fills an ABSENT switch; it never overrides a No."""
    off = {**_SAVED_BEFORE, "worker_auth_down": False}
    assert await _delivered(sf, client, saved_matrix=off) == []
