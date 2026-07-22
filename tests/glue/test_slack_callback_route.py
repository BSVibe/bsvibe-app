"""Route wiring for the slack inbound block_actions branch.

Pins that ``POST /api/webhooks/slack/{token}`` with a valid signature and a
form-encoded ``payload=<block_actions JSON>`` body is routed to the callback
handler (200 + callback marker) and does NOT land a TriggerEvent (it stays out of
intake), while a forged signature is rejected 401 BEFORE the handler is ever
reached. The callback handler itself is unit-tested in
``tests/connectors/test_slack_callback.py``; here we mock it to assert the route
seam + signature gate.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
import urllib.parse
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
# For slack the account's decrypted signing_secret is the inbound HMAC secret.
SIGNING_SECRET = "slack-signing-secret"  # noqa: S105 — test fixture

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
                connector="slack",
                webhook_token=token,
                signing_secret_ciphertext=cipher.encrypt(SIGNING_SECRET),
                delivery_config={"channel": "C1", "authorized_user_ids": ["U1"]},
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


def _interaction_body() -> bytes:
    payload = {
        "type": "block_actions",
        "user": {"id": "U1", "team_id": "T1"},
        "team": {"id": "T1"},
        "channel": {"id": "C1"},
        "message": {"ts": "1.1", "blocks": [{"type": "section"}, {"type": "actions"}]},
        "response_url": "https://hooks.slack.com/actions/T1/1/x",
        "actions": [{"value": "apv:" + str(uuid.uuid4()), "action_id": "apv:x"}],
    }
    return urllib.parse.urlencode({"payload": json.dumps(payload)}).encode()


def _sign(body: bytes, ts: str, secret: str = SIGNING_SECRET) -> str:
    base = b"v0:" + ts.encode() + b":" + body
    return "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()


def _headers(body: bytes, *, secret: str = SIGNING_SECRET) -> dict[str, str]:
    ts = str(int(time.time()))
    return {
        "X-Slack-Request-Timestamp": ts,
        "X-Slack-Signature": _sign(body, ts, secret),
        "Content-Type": "application/x-www-form-urlencoded",
    }


async def _trigger_count(sf: async_sessionmaker[AsyncSession]) -> int:
    async with sf() as s:
        return len((await s.execute(select(TriggerEventRow))).scalars().all())


async def test_valid_signature_interaction_routes_to_handler_and_skips_intake(
    client: httpx.AsyncClient,
    sf: async_sessionmaker[AsyncSession],
    seeded_token: str,
    monkeypatch,
) -> None:
    mock = AsyncMock(return_value=True)
    monkeypatch.setattr("backend.connectors.slack_callback.process_slack_callback", mock)

    body = _interaction_body()
    resp = await client.post(
        f"/api/webhooks/slack/{seeded_token}", content=body, headers=_headers(body)
    )

    assert resp.status_code == 200, resp.text
    assert resp.json().get("callback") is True
    mock.assert_awaited_once()
    # A block_actions tap is NOT a run — nothing lands on the intake path.
    assert await _trigger_count(sf) == 0


async def test_forged_signature_is_401_before_handler(
    client: httpx.AsyncClient,
    seeded_token: str,
    monkeypatch,
) -> None:
    mock = AsyncMock(return_value=True)
    monkeypatch.setattr("backend.connectors.slack_callback.process_slack_callback", mock)

    body = _interaction_body()
    resp = await client.post(
        f"/api/webhooks/slack/{seeded_token}",
        content=body,
        headers=_headers(body, secret="WRONG"),
    )

    assert resp.status_code == 401, resp.text
    mock.assert_not_awaited()  # signature gate runs BEFORE the callback handler
