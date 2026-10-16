"""#928 step 2 — Paddle's subscription webhook moves a workspace between plans.

``POST /api/webhooks/paddle`` is public; what makes a delivery Paddle's is the
``Paddle-Signature`` header — ``ts=<unix>;h1=<hex HMAC-SHA256(secret, "<ts>:<raw body>")>``
(Paddle Billing's documented scheme). Everything here is signed with a test secret the way
Paddle signs, and sent through the real app.

The propositions:

* only a correctly signed, fresh delivery moves a plan (forged → 401, stale → 401,
  no secret configured → 404 so the endpoint does not exist on a deployment that sells nothing);
* ``active``/``trialing`` on a Pro price → Pro; ``canceled``/``paused`` → Free; ``past_due`` →
  no change (Paddle is still collecting; it sends ``canceled`` if it gives up);
* the workspace comes from ``custom_data.workspace_id`` on the FIRST event and from the stored
  subscription id afterwards — a later event cannot re-point a subscription at another
  workspace;
* Paddle retries and does not order deliveries: a replayed ``event_id`` applies once, and an
  event older than the last one applied is ignored;
* the operator's workspace is never moved by a payment.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import backend.identity.db  # noqa: F401
import backend.identity.workspaces_db  # noqa: F401
from backend.api.deps import get_db_session
from backend.api.main import create_app
from backend.config import get_settings
from backend.data.rls import workspace_session_scope
from backend.identity.workspaces_db import WorkspaceRow

from .._support import db_engine

pytestmark = pytest.mark.asyncio

_SECRET = "pdl_ntfset_test_secret"
_PRO_PRICE = "pri_pro_monthly"


@pytest_asyncio.fixture
async def sf(monkeypatch) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    monkeypatch.setenv("BSVIBE_PADDLE_WEBHOOK_SECRET", _SECRET)
    monkeypatch.setenv("BSVIBE_PADDLE_PRO_PRICE_IDS", f"{_PRO_PRICE},pri_pro_yearly")
    get_settings.cache_clear()
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)
    get_settings.cache_clear()


@pytest_asyncio.fixture
async def client(sf) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app()

    async def _session() -> AsyncIterator[AsyncSession]:
        async with sf() as s:
            yield s

    app.dependency_overrides[get_db_session] = _session
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _workspace(sf, *, operator: bool = False) -> uuid.UUID:
    ws = uuid.uuid4()
    now = datetime.now(UTC)
    async with sf() as s, workspace_session_scope(s, ws):
        s.add(WorkspaceRow(id=ws, name="w", created_at=now, updated_at=now))
        await s.flush()
        if operator:
            await s.execute(
                update(WorkspaceRow)
                .where(WorkspaceRow.id == ws)
                .values(plan="operator", max_concurrent_runs=None, monthly_token_budget=None)
            )
        await s.commit()
    return ws


async def _row(sf, ws: uuid.UUID) -> WorkspaceRow:
    async with sf() as s, workspace_session_scope(s, ws):
        return (await s.execute(select(WorkspaceRow).where(WorkspaceRow.id == ws))).scalar_one()


_T0 = datetime(2026, 10, 16, 3, 0, tzinfo=UTC)


def _event(
    event_type: str,
    *,
    status: str,
    workspace_id: uuid.UUID | None,
    subscription_id: str = "sub_01",
    price_id: str = _PRO_PRICE,
    occurred_at: datetime = _T0,
    event_id: str | None = None,
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": subscription_id,
        "status": status,
        "customer_id": "ctm_01",
        "items": [{"price": {"id": price_id}, "quantity": 1}],
    }
    if workspace_id is not None:
        data["custom_data"] = {"workspace_id": str(workspace_id)}
    return {
        "event_id": event_id or f"evt_{uuid.uuid4().hex}",
        "event_type": event_type,
        "occurred_at": occurred_at.isoformat().replace("+00:00", "Z"),
        "data": data,
    }


def _signed(body: bytes, *, secret: str = _SECRET, ts: int | None = None) -> dict[str, str]:
    stamp = str(ts if ts is not None else int(time.time()))
    digest = hmac.new(secret.encode(), f"{stamp}:".encode() + body, hashlib.sha256).hexdigest()
    return {"Paddle-Signature": f"ts={stamp};h1={digest}", "Content-Type": "application/json"}


async def _post(client: httpx.AsyncClient, event: dict[str, Any], **sign: Any) -> httpx.Response:
    body = json.dumps(event).encode()
    return await client.post("/api/webhooks/paddle", content=body, headers=_signed(body, **sign))


# ── Signature ────────────────────────────────────────────────────────────────


async def test_an_activation_puts_the_workspace_on_pro(sf, client) -> None:
    ws = await _workspace(sf)

    r = await _post(client, _event("subscription.activated", status="active", workspace_id=ws))

    assert r.status_code == 200, r.text
    row = await _row(sf, ws)
    assert (row.plan, row.max_concurrent_runs, row.verify_stack_slots) == ("pro", 10, 2)
    assert (row.billing_subscription_id, row.billing_customer_id) == ("sub_01", "ctm_01")


async def test_a_forged_signature_is_401_and_moves_nothing(sf, client) -> None:
    ws = await _workspace(sf)
    event = _event("subscription.activated", status="active", workspace_id=ws)

    r = await _post(client, event, secret="not-the-secret")

    assert r.status_code == 401
    assert (await _row(sf, ws)).plan == "free"


async def test_a_stale_signature_is_401(sf, client) -> None:
    """A captured delivery replayed later carries its old timestamp."""
    ws = await _workspace(sf)
    event = _event("subscription.activated", status="active", workspace_id=ws)

    r = await _post(client, event, ts=int(time.time()) - 3600)

    assert r.status_code == 401
    assert (await _row(sf, ws)).plan == "free"


async def test_a_tampered_body_is_401(sf, client) -> None:
    ws = await _workspace(sf)
    body = json.dumps(_event("subscription.activated", status="active", workspace_id=ws)).encode()
    headers = _signed(body)

    r = await client.post(
        "/api/webhooks/paddle", content=body.replace(b'"active"', b'"trialing"'), headers=headers
    )

    assert r.status_code == 401


async def test_no_secret_means_no_endpoint(sf, client, monkeypatch) -> None:
    monkeypatch.setenv("BSVIBE_PADDLE_WEBHOOK_SECRET", "")
    get_settings.cache_clear()
    ws = await _workspace(sf)

    r = await _post(client, _event("subscription.activated", status="active", workspace_id=ws))

    assert r.status_code == 404
    assert (await _row(sf, ws)).plan == "free"


# ── Status → plan ────────────────────────────────────────────────────────────


async def test_a_cancellation_returns_the_workspace_to_free(sf, client) -> None:
    ws = await _workspace(sf)
    await _post(client, _event("subscription.activated", status="active", workspace_id=ws))

    r = await _post(
        client,
        _event(
            "subscription.canceled",
            status="canceled",
            workspace_id=None,
            occurred_at=_T0 + timedelta(days=30),
        ),
    )

    assert r.status_code == 200
    row = await _row(sf, ws)
    assert (row.plan, row.max_concurrent_runs, row.verify_stack_slots) == ("free", 3, 1)


async def test_past_due_keeps_the_plan_while_paddle_collects(sf, client) -> None:
    ws = await _workspace(sf)
    await _post(client, _event("subscription.activated", status="active", workspace_id=ws))

    await _post(
        client,
        _event(
            "subscription.past_due",
            status="past_due",
            workspace_id=None,
            occurred_at=_T0 + timedelta(days=30),
        ),
    )

    row = await _row(sf, ws)
    assert (row.plan, row.billing_status) == ("pro", "past_due")


async def test_an_active_subscription_on_an_unknown_price_moves_nothing(sf, client) -> None:
    ws = await _workspace(sf)

    r = await _post(
        client,
        _event("subscription.activated", status="active", workspace_id=ws, price_id="pri_other"),
    )

    assert r.status_code == 200
    assert (await _row(sf, ws)).plan == "free"


async def test_the_operator_workspace_is_never_moved_by_a_payment(sf, client) -> None:
    ws = await _workspace(sf, operator=True)

    await _post(client, _event("subscription.activated", status="active", workspace_id=ws))
    await _post(
        client,
        _event(
            "subscription.canceled",
            status="canceled",
            workspace_id=None,
            occurred_at=_T0 + timedelta(days=1),
        ),
    )

    row = await _row(sf, ws)
    assert (row.plan, row.max_concurrent_runs) == ("operator", None)


# ── Which workspace ──────────────────────────────────────────────────────────


async def test_a_later_event_cannot_repoint_the_subscription(sf, client) -> None:
    """``custom_data`` is set by the client at checkout; once a subscription is bound, its id is
    what names the workspace."""
    ws, other = await _workspace(sf), await _workspace(sf)
    await _post(client, _event("subscription.activated", status="active", workspace_id=ws))

    await _post(
        client,
        _event(
            "subscription.updated",
            status="active",
            workspace_id=other,
            occurred_at=_T0 + timedelta(hours=1),
        ),
    )

    assert (await _row(sf, ws)).plan == "pro"
    assert (await _row(sf, other)).plan == "free"


async def test_an_unknown_workspace_is_acknowledged_and_moves_nothing(sf, client) -> None:
    """A retry cannot make the workspace exist — answer 200 so Paddle stops, and log it."""
    r = await _post(
        client, _event("subscription.activated", status="active", workspace_id=uuid.uuid4())
    )

    assert r.status_code == 200


async def test_an_event_naming_no_workspace_and_no_known_subscription_moves_nothing(
    sf, client
) -> None:
    ws = await _workspace(sf)

    r = await _post(client, _event("subscription.activated", status="active", workspace_id=None))

    assert r.status_code == 200
    assert (await _row(sf, ws)).plan == "free"


# ── Retries and order ────────────────────────────────────────────────────────


async def test_a_replayed_event_applies_once(sf, client) -> None:
    ws = await _workspace(sf)
    activation = _event(
        "subscription.activated", status="active", workspace_id=ws, event_id="evt_same"
    )
    await _post(client, activation)
    await _post(
        client,
        _event(
            "subscription.canceled",
            status="canceled",
            workspace_id=None,
            occurred_at=_T0 + timedelta(days=30),
        ),
    )

    r = await _post(client, activation)  # Paddle's retry of the first delivery

    assert r.status_code == 200
    assert (await _row(sf, ws)).plan == "free"


async def test_an_older_event_arriving_late_is_ignored(sf, client) -> None:
    ws = await _workspace(sf)
    await _post(client, _event("subscription.activated", status="active", workspace_id=ws))
    await _post(
        client,
        _event(
            "subscription.canceled",
            status="canceled",
            workspace_id=None,
            occurred_at=_T0 + timedelta(days=30),
        ),
    )

    await _post(
        client,
        _event(
            "subscription.updated",
            status="active",
            workspace_id=None,
            occurred_at=_T0 + timedelta(days=2),
        ),
    )

    assert (await _row(sf, ws)).plan == "free"
