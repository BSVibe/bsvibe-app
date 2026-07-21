"""Route wiring for the telegram inbound callback_query branch.

Pins that ``POST /api/webhooks/telegram/{token}`` with a valid secret-token header
and a ``callback_query`` body is routed to the callback handler (200 + callback
marker) and does NOT land a TriggerEvent (it stays out of intake), while a forged
secret is rejected 401 BEFORE the handler is ever reached. The callback handler
itself is unit-tested in ``tests/connectors/test_telegram_callback.py``; here we
mock it to assert the route seam + secret gate.
"""

from __future__ import annotations

import base64
import json
import os
import uuid
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.api.deps import get_db_session
from backend.api.main import create_app
from backend.api.webhooks import get_credential_cipher
from backend.connectors.db import ConnectorAccountRow
from backend.router.accounts.crypto import CredentialCipher
from backend.workflow.infrastructure.intake.db import TriggerEventRow

from .._support import db_engine

TEST_KEY = b"0123456789abcdef0123456789abcdef"
SECRET = "tg-secret-token"

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def sf():
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def cipher() -> CredentialCipher:
    return CredentialCipher(TEST_KEY)


@pytest_asyncio.fixture
async def seeded_token(sf: async_sessionmaker[AsyncSession], cipher: CredentialCipher) -> str:
    token = "wht_" + base64.urlsafe_b64encode(os.urandom(24)).decode().rstrip("=")
    async with sf() as s:
        s.add(
            ConnectorAccountRow(
                id=uuid.uuid4(),
                workspace_id=uuid.uuid4(),
                connector="telegram",
                webhook_token=token,
                signing_secret_ciphertext=cipher.encrypt(SECRET),
                delivery_config={"chat_id": "42"},
                is_active=True,
            )
        )
        await s.commit()
    return token


@pytest_asyncio.fixture
async def client(sf, cipher: CredentialCipher):
    app = create_app()

    async def _session():
        async with sf() as s:
            yield s

    app.dependency_overrides[get_db_session] = _session
    app.dependency_overrides[get_credential_cipher] = lambda: cipher
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _callback_body() -> bytes:
    return json.dumps(
        {
            "update_id": 700,
            "callback_query": {
                "id": "cbq",
                "from": {"id": 42},
                "message": {"message_id": 9, "chat": {"id": 42, "type": "private"}},
                "data": "apv:" + str(uuid.uuid4()),
            },
        }
    ).encode()


def _headers(secret: str = SECRET) -> dict[str, str]:
    return {
        "X-Telegram-Bot-Api-Secret-Token": secret,
        "Content-Type": "application/json",
    }


async def _trigger_count(sf: async_sessionmaker[AsyncSession]) -> int:
    async with sf() as s:
        return len((await s.execute(select(TriggerEventRow))).scalars().all())


async def test_valid_secret_callback_routes_to_handler_and_skips_intake(
    client: httpx.AsyncClient,
    sf: async_sessionmaker[AsyncSession],
    seeded_token: str,
    monkeypatch,
) -> None:
    mock = AsyncMock(return_value=True)
    monkeypatch.setattr("backend.connectors.telegram_callback.process_telegram_callback", mock)

    resp = await client.post(
        f"/api/webhooks/telegram/{seeded_token}",
        content=_callback_body(),
        headers=_headers(),
    )

    assert resp.status_code == 200, resp.text
    assert resp.json().get("callback") is True
    mock.assert_awaited_once()
    # A callback_query is NOT a run — nothing lands on the intake path.
    assert await _trigger_count(sf) == 0


async def test_forged_secret_is_401_before_handler(
    client: httpx.AsyncClient,
    seeded_token: str,
    monkeypatch,
) -> None:
    mock = AsyncMock(return_value=True)
    monkeypatch.setattr("backend.connectors.telegram_callback.process_telegram_callback", mock)

    resp = await client.post(
        f"/api/webhooks/telegram/{seeded_token}",
        content=_callback_body(),
        headers=_headers(secret="WRONG"),
    )

    assert resp.status_code == 401, resp.text
    mock.assert_not_awaited()  # secret gate runs BEFORE the callback handler
