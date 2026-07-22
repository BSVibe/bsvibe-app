"""Route wiring for the discord inbound component-interaction branch.

Pins that ``POST /api/webhooks/discord/{token}`` with a valid Ed25519 signature
and a component-tap (type 3, ``custom_id`` apv/rej) body is routed to the discord
callback, which returns its OWN response — a synchronous DEFERRED (type 6) reply
with the approval scheduled on a background task — and does NOT land a TriggerEvent
(it stays out of intake). A forged signature is rejected 401 BEFORE the handler is
ever reached. A registration PING (type 1) still answers with a PONG (type 1). The
callback handler + background timing are unit-tested in
``tests/connectors/test_discord_callback.py``; here we pin the route seam +
signature gate + the connector-owns-its-response contract.
"""

from __future__ import annotations

import base64
import json
import os
import uuid

import httpx
import pytest
import pytest_asyncio
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.responses import JSONResponse
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
TIMESTAMP = "1700000000"

pytestmark = pytest.mark.asyncio


def _keypair() -> tuple[Ed25519PrivateKey, str]:
    private = Ed25519PrivateKey.generate()
    return private, private.public_key().public_bytes_raw().hex()


# One shared keypair for the whole module: the account stores the public key (as
# its "signing secret"); requests are signed with the private key.
PRIVATE, PUBLIC_HEX = _keypair()


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
        # For discord the account's decrypted "signing secret" is the Ed25519
        # public key (hex) — the resolver hands it to the parser as ``public_key``.
        s.add(
            ConnectorAccountRow(
                id=uuid.uuid4(),
                workspace_id=uuid.uuid4(),
                connector="discord",
                webhook_token=token,
                signing_secret_ciphertext=cipher.encrypt(PUBLIC_HEX),
                delivery_config={"channel_id": "CH1", "authorized_user_ids": ["U1"]},
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


def _component_body() -> bytes:
    return json.dumps(
        {
            "id": "int-1",
            "application_id": "APP1",
            "type": 3,
            "token": "itoken-1",
            "guild_id": "G1",
            "channel_id": "CH1",
            "member": {"user": {"id": "U1", "bot": False}},
            "message": {"id": "M1", "content": "작업 완료"},
            "data": {"custom_id": "apv:" + str(uuid.uuid4()), "component_type": 2},
        }
    ).encode()


def _ping_body() -> bytes:
    return json.dumps({"id": "1", "type": 1}).encode()


def _sign(body: bytes, ts: str = TIMESTAMP) -> str:
    return PRIVATE.sign(ts.encode() + body).hex()


def _headers(body: bytes, *, signature: str | None = None, ts: str = TIMESTAMP) -> dict[str, str]:
    return {
        "X-Signature-Ed25519": signature if signature is not None else _sign(body, ts),
        "X-Signature-Timestamp": ts,
        "Content-Type": "application/json",
    }


async def _trigger_count(sf: async_sessionmaker[AsyncSession]) -> int:
    async with sf() as s:
        return len((await s.execute(select(TriggerEventRow))).scalars().all())


async def test_valid_signature_component_tap_returns_type6_and_skips_intake(
    client: httpx.AsyncClient,
    sf: async_sessionmaker[AsyncSession],
    seeded_token: str,
    monkeypatch,
) -> None:
    # Mock the callback so the (slow) background approval never really runs here —
    # this test pins ONLY the route seam: the connector returns its own Response.
    async def _fake_process(**_kwargs):
        return JSONResponse(status_code=200, content={"type": 6})

    monkeypatch.setattr(
        "backend.connectors.discord_callback.process_discord_callback", _fake_process
    )

    body = _component_body()
    resp = await client.post(
        f"/api/webhooks/discord/{seeded_token}", content=body, headers=_headers(body)
    )

    assert resp.status_code == 200, resp.text
    # The DEFERRED interaction response is returned synchronously (type 6).
    assert resp.json() == {"type": 6}
    # A component tap is NOT a run — nothing lands on the intake path.
    assert await _trigger_count(sf) == 0


async def test_forged_signature_is_401_before_handler(
    client: httpx.AsyncClient,
    seeded_token: str,
    monkeypatch,
) -> None:
    called = {"hit": False}

    async def _fake_process(**_kwargs):
        called["hit"] = True
        return JSONResponse(status_code=200, content={"type": 6})

    monkeypatch.setattr(
        "backend.connectors.discord_callback.process_discord_callback", _fake_process
    )

    body = _component_body()
    other_private, _ = _keypair()
    forged = other_private.sign(TIMESTAMP.encode() + body).hex()
    resp = await client.post(
        f"/api/webhooks/discord/{seeded_token}",
        content=body,
        headers=_headers(body, signature=forged),
    )

    assert resp.status_code == 401, resp.text
    assert called["hit"] is False  # signature gate runs BEFORE the callback handler


async def test_valid_signature_ping_returns_pong(
    client: httpx.AsyncClient,
    seeded_token: str,
) -> None:
    # A registration PING still verifies, then the route answers with a PONG.
    body = _ping_body()
    resp = await client.post(
        f"/api/webhooks/discord/{seeded_token}", content=body, headers=_headers(body)
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"type": 1}
