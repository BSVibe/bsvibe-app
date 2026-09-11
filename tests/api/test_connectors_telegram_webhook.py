"""``POST /api/v1/connectors/{id}/webhook`` — register telegram's ingress.

Prod 2026-09-11: Telegram answered ``url_set: False`` for the live bot, with no
``last_error_*`` fields at all — it had never attempted a delivery, so nothing
was blocking it. **No webhook was registered**, and ``setWebhook`` appeared
nowhere in the repo, so nothing could restore one. The founder kept receiving
approve/deny cards (outbound is unaffected) whose buttons did nothing.

The connector already existed, so a fix that only wired registration into
*create* would not have repaired it. This route is the repair, and create calls
the same function so the gap does not reopen for the next connector.

Route shape mirrors ``POST /{id}/import``: workspace-scoped resolve, 404 for
anything not ours (a revoked row looks the same as missing, exactly as the
public ingress does), 422 for a connector the action cannot apply to.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.api.deps import get_current_user, get_db_session, get_workspace_id
from backend.api.main import create_app
from backend.api.v1.connectors import get_telegram_client_factory
from backend.api.webhooks import get_credential_cipher
from backend.connectors.db import ConnectorAccountRow
from backend.router.accounts.crypto import CredentialCipher

from .._support import db_engine, fake_current_user

TEST_KEY = b"0123456789abcdef0123456789abcdef"

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def sf():
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def cipher() -> CredentialCipher:
    return CredentialCipher(TEST_KEY)


@pytest.fixture
def workspace_id() -> uuid.UUID:
    return uuid.uuid4()


class _FakeTelegram:
    def __init__(self, *, fail: Exception | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._fail = fail
        self._url = ""

    async def set_webhook(self, url: str, *, secret_token: str) -> dict[str, Any]:
        if self._fail is not None:
            raise self._fail
        self.calls.append({"url": url, "secret_token": secret_token})
        self._url = url
        return {"ok": True}

    async def get_webhook_info(self) -> dict[str, Any]:
        return {"url": self._url, "pending_update_count": 0}


def _make_client(
    app,
    sf: async_sessionmaker[AsyncSession],
    cipher: CredentialCipher,
    ws: uuid.UUID,
    telegram: Any,
) -> httpx.AsyncClient:
    async def _session():
        async with sf() as s:
            yield s

    app.dependency_overrides[get_current_user] = fake_current_user()
    app.dependency_overrides[get_workspace_id] = lambda: ws
    app.dependency_overrides[get_db_session] = _session
    app.dependency_overrides[get_credential_cipher] = lambda: cipher
    app.dependency_overrides[get_telegram_client_factory] = lambda: lambda _token: telegram
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def _create(client: httpx.AsyncClient, connector: str, **extra: Any) -> dict[str, Any]:
    payload = {"connector": connector, "signing_secret": "bot:token", **extra}
    r = await client.post("/api/v1/connectors", json=payload)
    assert r.status_code == 201, r.text
    return r.json()


async def test_register_points_telegram_at_this_workspaces_ingress(sf, cipher, workspace_id):
    """The repair path for a connector that already exists."""
    app = create_app()
    tg = _FakeTelegram()
    async with _make_client(app, sf, cipher, workspace_id, tg) as client:
        created = await _create(client, "telegram", delivery_config={"chat_id": "42"})
        tg.calls.clear()  # ignore whatever create did; this asserts the REPAIR route

        r = await client.post(f"/api/v1/connectors/{created['id']}/webhook")
        assert r.status_code == 200, r.text
        body = r.json()

    assert body["registered"] is True
    assert len(tg.calls) == 1
    assert tg.calls[0]["url"].endswith(f"/api/webhooks/telegram/{created['webhook_token']}")
    assert ":" not in tg.calls[0]["secret_token"]


async def test_the_registered_secret_is_never_returned(sf, cipher, workspace_id):
    """``webhook_secret`` is a live credential.

    It already sits in ``SECRET_DELIVERY_KEYS``, so the redaction exists — this
    pins that minting one does not sneak it out through a NEW response shape.
    """
    app = create_app()
    tg = _FakeTelegram()
    async with _make_client(app, sf, cipher, workspace_id, tg) as client:
        created = await _create(client, "telegram", delivery_config={"chat_id": "42"})
        r = await client.post(f"/api/v1/connectors/{created['id']}/webhook")
        assert r.status_code == 200, r.text
        secret = tg.calls[-1]["secret_token"]
        assert secret not in r.text

        listed = await client.get("/api/v1/connectors")
        assert secret not in listed.text


async def test_creating_a_telegram_connector_registers_it(sf, cipher, workspace_id):
    """The gap must not reopen for the next connector.

    Repair alone would leave every future telegram binding in the same
    never-registered state prod spent weeks in.
    """
    app = create_app()
    tg = _FakeTelegram()
    async with _make_client(app, sf, cipher, workspace_id, tg) as client:
        created = await _create(client, "telegram", delivery_config={"chat_id": "42"})

    assert len(tg.calls) == 1
    assert tg.calls[0]["url"].endswith(f"/api/webhooks/telegram/{created['webhook_token']}")


async def test_a_telegram_api_failure_does_not_lose_the_connector(sf, cipher, workspace_id):
    """Create must still 201 when Telegram is unreachable.

    Otherwise a Telegram blip destroys a binding the founder just configured —
    and the registration is separately repairable by the route above, so there
    is nothing to gain by failing the whole create.
    """
    app = create_app()
    tg = _FakeTelegram(fail=RuntimeError("telegram is down"))
    async with _make_client(app, sf, cipher, workspace_id, tg) as client:
        created = await _create(client, "telegram", delivery_config={"chat_id": "42"})
        assert created["id"]

        # The repair route, by contrast, must report the failure — it exists to
        # answer "is it registered?", so swallowing there would be a lie.
        r = await client.post(f"/api/v1/connectors/{created['id']}/webhook")
        assert r.status_code == 502, r.text


async def test_a_non_telegram_connector_is_refused(sf, cipher, workspace_id):
    """Slack/GitHub webhooks are configured in their own consoles — there is no
    Bot API to call, and pretending otherwise would report a success that never
    happened."""
    app = create_app()
    tg = _FakeTelegram()
    async with _make_client(app, sf, cipher, workspace_id, tg) as client:
        created = await _create(client, "slack")
        r = await client.post(f"/api/v1/connectors/{created['id']}/webhook")
        assert r.status_code == 422, r.text
    assert tg.calls == []


async def test_another_workspaces_connector_is_not_found(sf, cipher, workspace_id):
    """Workspace isolation on the same path every other connector route uses."""
    app = create_app()
    tg = _FakeTelegram()
    async with _make_client(app, sf, cipher, workspace_id, tg) as client:
        created = await _create(client, "telegram", delivery_config={"chat_id": "42"})

    other = create_app()
    async with _make_client(other, sf, cipher, uuid.uuid4(), tg) as stranger:
        r = await stranger.post(f"/api/v1/connectors/{created['id']}/webhook")
        assert r.status_code == 404, r.text


async def test_a_revoked_connector_is_not_found(sf, cipher, workspace_id):
    """A soft-revoked row looks the same as missing, mirroring the ingress."""
    app = create_app()
    tg = _FakeTelegram()
    async with _make_client(app, sf, cipher, workspace_id, tg) as client:
        created = await _create(client, "telegram", delivery_config={"chat_id": "42"})
        assert (await client.delete(f"/api/v1/connectors/{created['id']}")).status_code == 204
        r = await client.post(f"/api/v1/connectors/{created['id']}/webhook")
        assert r.status_code == 404, r.text


async def test_the_stored_secret_matches_what_telegram_was_given(sf, cipher, workspace_id):
    """The proposition the whole change rests on.

    The resolver verifies inbound updates against the STORED secret; Telegram
    echoes the REGISTERED one. If those two ever diverge, every update is
    rejected and the webhook is registered but useless — which is exactly the
    state a naive "just call setWebhook" fix would have produced.
    """
    app = create_app()
    tg = _FakeTelegram()
    async with _make_client(app, sf, cipher, workspace_id, tg) as client:
        created = await _create(client, "telegram", delivery_config={"chat_id": "42"})
        await client.post(f"/api/v1/connectors/{created['id']}/webhook")

    registered = tg.calls[-1]["secret_token"]
    async with sf() as s:
        row = await s.get(ConnectorAccountRow, uuid.UUID(created["id"]))
        assert row is not None
        assert row.delivery_config["webhook_secret"] == registered
