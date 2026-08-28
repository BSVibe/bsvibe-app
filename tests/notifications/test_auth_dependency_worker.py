"""AuthDependencyWorker — tell the founder when nobody can sign in.

Prod 2026-08-28: the Supabase project backing ``USER_JWT_JWKS_URL`` was paused
(free-tier idle), which removes its subdomain from DNS. Sign-in returned 500,
every ``get_current_user`` route 401'd, and NOTHING noticed — the host uptime
probe calls any HTTP response healthy, and the founder had been working through
MCP, whose tokens are verified against local keys and never touch that host.

The failure was invisible because the only passive signal is a real sign-in
failing, and during a quiet stretch there are none. So this worker probes.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import backend.identity.workspaces_db  # noqa: F401
import backend.notifications.db  # noqa: F401
from backend.identity.workspaces_db import WorkspaceRow
from backend.notifications.db import NotificationEventRow
from backend.shared.authz.probe import UserKeySourceStatus
from backend.workflow.infrastructure.workers.auth_dependency_worker import (
    AuthDependencyWorker,
)

from .._support import db_engine

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def sf() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


async def _seed_workspaces(sf, n: int = 2) -> list[uuid.UUID]:
    ids = [uuid.uuid4() for _ in range(n)]
    async with sf() as s:
        for i, ws in enumerate(ids):
            s.add(WorkspaceRow(id=ws, name=f"ws{i}"))
        await s.commit()
    return ids


async def _events(sf) -> list[NotificationEventRow]:
    async with sf() as s:
        return list((await s.execute(select(NotificationEventRow))).scalars().all())


def _worker(sf, statuses: list[UserKeySourceStatus]) -> AuthDependencyWorker:
    """A worker whose probe returns ``statuses`` in order (last one repeats)."""
    seq = list(statuses)

    async def _probe() -> UserKeySourceStatus:
        return seq.pop(0) if len(seq) > 1 else seq[0]

    return AuthDependencyWorker(session_factory=sf, probe=_probe, today=lambda: "2026-08-28")


DOWN = UserKeySourceStatus(ok=False, source="jwks_url", detail="Name or service not known")
UP = UserKeySourceStatus(ok=True, source="jwks_url")


async def test_a_down_dependency_notifies_every_workspace(sf) -> None:
    """Sign-in is deployment-wide — every workspace is locked out at once."""
    ids = await _seed_workspaces(sf, 2)

    await self_tick(_worker(sf, [DOWN]))

    rows = await _events(sf)
    assert {r.workspace_id for r in rows} == set(ids)
    assert {r.event for r in rows} == {"auth_down"}
    assert "Name or service not known" in rows[0].payload["body"]


async def test_a_healthy_dependency_notifies_nobody(sf) -> None:
    """Positive control — the quiet path must stay quiet."""
    await _seed_workspaces(sf, 2)

    await self_tick(_worker(sf, [UP]))

    assert await _events(sf) == []


async def test_repeated_ticks_while_down_do_not_spam(sf) -> None:
    """One alert per workspace per day — the dedupe key carries the date."""
    await _seed_workspaces(sf, 1)
    worker = _worker(sf, [DOWN])

    await self_tick(worker)
    await self_tick(worker)
    await self_tick(worker)

    assert len(await _events(sf)) == 1


async def test_recovery_is_announced_once(sf) -> None:
    """Silence after an alert is indistinguishable from a still-broken system."""
    await _seed_workspaces(sf, 1)
    worker = _worker(sf, [DOWN, UP])

    await self_tick(worker)  # down  → alert
    await self_tick(worker)  # up    → recovery
    await self_tick(worker)  # up    → nothing more

    rows = sorted(await _events(sf), key=lambda r: r.dedupe_key)
    assert len(rows) == 2
    assert any(r.payload.get("recovered") for r in rows)


async def test_recovery_without_a_prior_alert_is_silent(sf) -> None:
    """A restart must not announce a recovery from an outage it never saw."""
    await _seed_workspaces(sf, 1)

    await self_tick(_worker(sf, [UP]))

    assert await _events(sf) == []


async def self_tick(worker: AuthDependencyWorker) -> None:
    await worker.check_once()


async def test_the_recovery_message_does_not_say_it_is_down(sf) -> None:
    """A recovery notification titled "Sign-in is down" is a lie on the phone.

    The founder reads the title on a lock screen and never opens it. Reusing the
    outage copy for the all-clear is exactly the kind of false statement this
    worker exists to prevent.
    """
    await _seed_workspaces(sf, 1)
    worker = _worker(sf, [DOWN, UP])

    await self_tick(worker)
    await self_tick(worker)

    rows = await _events(sf)
    recovery = next(r for r in rows if r.payload.get("recovered"))
    outage = next(r for r in rows if not r.payload.get("recovered"))

    assert "down" not in recovery.payload["title"].lower()
    assert recovery.payload["title"] != outage.payload["title"]
    assert recovery.payload["body"] != outage.payload["body"]


async def test_the_row_this_worker_emits_is_one_the_notify_worker_can_deliver(sf) -> None:
    """Seam test — the PRODUCER's real payload through the REAL consumer.

    Every test above stops at the outbox row, and the NotifyWorker tests seed
    their own hand-written payload. Between them sits the shape this worker
    actually writes (it carries extra ``recovered`` / ``source`` keys), which
    nothing had ever handed to the thing that renders and sends it.
    """
    from backend.connectors.db import ConnectorAccountRow
    from backend.workflow.infrastructure.workers.notify_worker import NotifyWorker

    ws = uuid.uuid4()
    async with sf() as s:
        s.add(WorkspaceRow(id=ws, name="ws", language="ko"))
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

    await self_tick(_worker(sf, [DOWN]))

    sent: list[str] = []

    class _Sender:
        async def send(self, *, connector, content, delivery_config, signing_secret_ciphertext):  # noqa: ANN001, ANN003
            assert content.title, "rendered an empty title"
            assert "Name or service not known" in content.body
            sent.append(connector)

    await NotifyWorker(session_factory=sf, sender=_Sender()).drain_once()

    assert sent == ["telegram"], "the producer's own payload never reached a channel"
