"""Connector-inbound end-to-end — external signed webhook → TriggerEvent.

The connector-inbound entrypoint (Workflow §11.2). A seeded
``connector_accounts`` row binds a connector + unguessable ``webhook_token``
to a workspace; an external provider POSTs a signed delivery to
``POST /api/webhooks/{connector}/{token}`` and we land a
``TriggerEvent(source=<connector>, trigger_kind=webhook)`` on the existing
intake path. This test proves the HTTP ingress + connector→workspace
resolution + signature verification + handshake answers + idempotency. The
downstream IntakeWorker → Request → ... → Safe Mode delivery path is covered
by the Direct-path e2e and is intentionally NOT re-exercised here.

The route is PUBLIC (no founder auth) so no auth override is needed; the
credential cipher and DB session are overridden onto an in-memory deterministic
setup. Runs on in-memory SQLite by default, real Postgres when
``BSVIBE_DATABASE_URL`` is set (mirrors the other glue tests).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
import uuid

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.api.deps import get_db_session
from backend.api.main import create_app
from backend.api.webhooks import get_credential_cipher
from backend.connectors.db import ConnectorAccountRow
from backend.intake.db import TriggerEventRow
from backend.router.accounts.crypto import CredentialCipher

from .._support import db_engine

# Deterministic 32-byte AES key for CredentialCipher in tests.
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


@pytest.fixture
def slack_secret() -> str:
    return "shhh-signing-secret"


@pytest_asyncio.fixture
async def seeded_token(
    sf: async_sessionmaker[AsyncSession],
    cipher: CredentialCipher,
    workspace_id: uuid.UUID,
    slack_secret: str,
) -> str:
    """Seed an active slack connector_account; return its webhook_token."""
    token = "wht_" + base64.urlsafe_b64encode(os.urandom(24)).decode().rstrip("=")
    async with sf() as s:
        s.add(
            ConnectorAccountRow(
                id=uuid.uuid4(),
                workspace_id=workspace_id,
                connector="slack",
                webhook_token=token,
                signing_secret_ciphertext=cipher.encrypt(slack_secret),
                external_ref="T012345/team",
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


# --------------------------------------------------------------------------
# Slack signing helper (mirrors the slack plugin's test helper)
# --------------------------------------------------------------------------


def _slack_sign(secret: str, timestamp: str, body: bytes) -> str:
    base = b"v0:" + timestamp.encode() + b":" + body
    return "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()


def _slack_headers(secret: str, body: bytes) -> dict[str, str]:
    ts = str(int(time.time()))
    return {
        "X-Slack-Request-Timestamp": ts,
        "X-Slack-Signature": _slack_sign(secret, ts, body),
        "Content-Type": "application/json",
    }


def _slack_event_body(event_id: str = "Ev123") -> bytes:
    return json.dumps(
        {
            "type": "event_callback",
            "event_id": event_id,
            "team_id": "T1",
            "event": {
                "type": "app_mention",
                "channel": "C9",
                "user": "U1",
                "text": "<@U0> ship the thing",
                "ts": "1700000000.000100",
            },
        }
    ).encode()


async def _trigger_events(
    sf: async_sessionmaker[AsyncSession], workspace_id: uuid.UUID
) -> list[TriggerEventRow]:
    async with sf() as s:
        rows = (
            (
                await s.execute(
                    select(TriggerEventRow).where(TriggerEventRow.workspace_id == workspace_id)
                )
            )
            .scalars()
            .all()
        )
    return list(rows)


# --------------------------------------------------------------------------
# The connector-inbound path
# --------------------------------------------------------------------------


async def test_signed_slack_event_lands_one_trigger_event(
    client: httpx.AsyncClient,
    sf: async_sessionmaker[AsyncSession],
    workspace_id: uuid.UUID,
    slack_secret: str,
    seeded_token: str,
) -> None:
    body = _slack_event_body()
    resp = await client.post(
        f"/api/webhooks/slack/{seeded_token}",
        content=body,
        headers=_slack_headers(slack_secret, body),
    )
    assert resp.status_code == 202, resp.text
    assert resp.json() == {"accepted": True, "duplicate": False}

    events = await _trigger_events(sf, workspace_id)
    assert len(events) == 1
    evt = events[0]
    assert evt.source == "slack"
    assert evt.workspace_id == workspace_id
    assert evt.trigger_kind.value == "webhook"
    assert evt.idempotency_key == "Ev123"
    assert evt.payload["slack_event"] == "app_mention"


async def test_bad_signature_is_401_no_trigger_event(
    client: httpx.AsyncClient,
    sf: async_sessionmaker[AsyncSession],
    workspace_id: uuid.UUID,
    seeded_token: str,
) -> None:
    body = _slack_event_body()
    # Sign with the wrong secret → verification fails.
    resp = await client.post(
        f"/api/webhooks/slack/{seeded_token}",
        content=body,
        headers=_slack_headers("WRONG-secret", body),
    )
    assert resp.status_code == 401, resp.text
    assert await _trigger_events(sf, workspace_id) == []


async def test_unknown_token_is_404(
    client: httpx.AsyncClient,
    slack_secret: str,
) -> None:
    body = _slack_event_body()
    resp = await client.post(
        "/api/webhooks/slack/does-not-exist",
        content=body,
        headers=_slack_headers(slack_secret, body),
    )
    assert resp.status_code == 404, resp.text


async def test_inactive_account_is_404(
    client: httpx.AsyncClient,
    sf: async_sessionmaker[AsyncSession],
    cipher: CredentialCipher,
    workspace_id: uuid.UUID,
    slack_secret: str,
) -> None:
    token = "wht_inactive"
    async with sf() as s:
        s.add(
            ConnectorAccountRow(
                id=uuid.uuid4(),
                workspace_id=workspace_id,
                connector="slack",
                webhook_token=token,
                signing_secret_ciphertext=cipher.encrypt(slack_secret),
                is_active=False,
            )
        )
        await s.commit()

    body = _slack_event_body()
    resp = await client.post(
        f"/api/webhooks/slack/{token}",
        content=body,
        headers=_slack_headers(slack_secret, body),
    )
    assert resp.status_code == 404, resp.text


async def test_unknown_connector_is_404(
    client: httpx.AsyncClient,
    slack_secret: str,
) -> None:
    body = b"{}"
    resp = await client.post(
        "/api/webhooks/pager-duty/whatever",
        content=body,
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 404, resp.text


async def test_slack_url_verification_echoes_challenge(
    client: httpx.AsyncClient,
    sf: async_sessionmaker[AsyncSession],
    workspace_id: uuid.UUID,
    slack_secret: str,
    seeded_token: str,
) -> None:
    body = json.dumps({"type": "url_verification", "challenge": "chal-token-xyz"}).encode()
    resp = await client.post(
        f"/api/webhooks/slack/{seeded_token}",
        content=body,
        headers=_slack_headers(slack_secret, body),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"challenge": "chal-token-xyz"}
    # A handshake is not a workflow trigger.
    assert await _trigger_events(sf, workspace_id) == []


async def test_redelivery_is_idempotent_single_trigger_event(
    client: httpx.AsyncClient,
    sf: async_sessionmaker[AsyncSession],
    workspace_id: uuid.UUID,
    slack_secret: str,
    seeded_token: str,
) -> None:
    body = _slack_event_body(event_id="EvDup")

    first = await client.post(
        f"/api/webhooks/slack/{seeded_token}",
        content=body,
        headers=_slack_headers(slack_secret, body),
    )
    assert first.status_code == 202
    assert first.json()["duplicate"] is False

    # Slack redelivers the same event (same event_id) — must collapse.
    second = await client.post(
        f"/api/webhooks/slack/{seeded_token}",
        content=body,
        headers=_slack_headers(slack_secret, body),
    )
    assert second.status_code == 202
    assert second.json()["duplicate"] is True

    events = await _trigger_events(sf, workspace_id)
    assert len(events) == 1


async def test_unsupported_event_accepted_but_no_trigger_event(
    client: httpx.AsyncClient,
    sf: async_sessionmaker[AsyncSession],
    workspace_id: uuid.UUID,
    slack_secret: str,
    seeded_token: str,
) -> None:
    """A verified-but-uninteresting delivery (reaction_added) → 202 skip, no row."""
    body = json.dumps(
        {
            "type": "event_callback",
            "event_id": "EvSkip",
            "event": {"type": "reaction_added", "channel": "C1"},
        }
    ).encode()
    resp = await client.post(
        f"/api/webhooks/slack/{seeded_token}",
        content=body,
        headers=_slack_headers(slack_secret, body),
    )
    assert resp.status_code == 202, resp.text
    assert resp.json() == {"accepted": True, "skipped": True}
    assert await _trigger_events(sf, workspace_id) == []


# --------------------------------------------------------------------------
# Sentry signing helpers (mirror the sentry plugin's test helper) + the
# connector-inbound path for the newly wired ``sentry`` connector.
# --------------------------------------------------------------------------

SENTRY_SECRET = "shhh-client-secret"


def _sentry_sign(secret: str, body: bytes) -> str:
    """Compute the bare-hex HMAC-SHA256 signature Sentry sends (no prefix)."""
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _sentry_headers(secret: str, body: bytes, *, resource: str = "issue") -> dict[str, str]:
    return {
        "Sentry-Hook-Signature": _sentry_sign(secret, body),
        "Sentry-Hook-Resource": resource,
        "Content-Type": "application/json",
    }


def _sentry_issue_body(issue_id: str = "100001", *, hook_id: str = "WH-1") -> bytes:
    return json.dumps(
        {
            "id": hook_id,
            "action": "created",
            "data": {
                "issue": {
                    "id": issue_id,
                    "title": "TypeError: undefined is not a function",
                    "culprit": "app/main.py in handler",
                    "level": "error",
                    "permalink": f"https://sentry.io/org/proj/issues/{issue_id}/",
                    "project": "proj",
                }
            },
        }
    ).encode()


@pytest_asyncio.fixture
async def seeded_sentry_token(
    sf: async_sessionmaker[AsyncSession],
    cipher: CredentialCipher,
    workspace_id: uuid.UUID,
) -> str:
    """Seed an active sentry connector_account; return its webhook_token."""
    token = "wht_" + base64.urlsafe_b64encode(os.urandom(24)).decode().rstrip("=")
    async with sf() as s:
        s.add(
            ConnectorAccountRow(
                id=uuid.uuid4(),
                workspace_id=workspace_id,
                connector="sentry",
                webhook_token=token,
                signing_secret_ciphertext=cipher.encrypt(SENTRY_SECRET),
                external_ref="org/proj",
                is_active=True,
            )
        )
        await s.commit()
    return token


async def test_signed_sentry_issue_lands_one_trigger_event(
    client: httpx.AsyncClient,
    sf: async_sessionmaker[AsyncSession],
    workspace_id: uuid.UUID,
    seeded_sentry_token: str,
) -> None:
    body = _sentry_issue_body("100001", hook_id="WH-42")
    resp = await client.post(
        f"/api/webhooks/sentry/{seeded_sentry_token}",
        content=body,
        headers=_sentry_headers(SENTRY_SECRET, body),
    )
    assert resp.status_code == 202, resp.text
    assert resp.json() == {"accepted": True, "duplicate": False}

    events = await _trigger_events(sf, workspace_id)
    assert len(events) == 1
    evt = events[0]
    assert evt.source == "sentry"
    assert evt.workspace_id == workspace_id
    assert evt.trigger_kind.value == "webhook"
    assert evt.idempotency_key == "sentry:issue:WH-42"
    assert evt.payload["sentry_resource"] == "issue"
    assert evt.payload["issue_id"] == "100001"


async def test_sentry_bad_signature_is_401_no_trigger_event(
    client: httpx.AsyncClient,
    sf: async_sessionmaker[AsyncSession],
    workspace_id: uuid.UUID,
    seeded_sentry_token: str,
) -> None:
    body = _sentry_issue_body()
    # Sign with the wrong secret → SentrySignatureError → 401.
    headers = _sentry_headers("WRONG-secret", body)
    resp = await client.post(
        f"/api/webhooks/sentry/{seeded_sentry_token}",
        content=body,
        headers=headers,
    )
    assert resp.status_code == 401, resp.text
    assert await _trigger_events(sf, workspace_id) == []


async def test_sentry_unsupported_resource_accepted_but_no_trigger_event(
    client: httpx.AsyncClient,
    sf: async_sessionmaker[AsyncSession],
    workspace_id: uuid.UUID,
    seeded_sentry_token: str,
) -> None:
    """A verified delivery whose ``Sentry-Hook-Resource`` is not acted on
    (e.g. ``installation``) → 202 benign skip, no TriggerEvent."""
    body = _sentry_issue_body()
    resp = await client.post(
        f"/api/webhooks/sentry/{seeded_sentry_token}",
        content=body,
        headers=_sentry_headers(SENTRY_SECRET, body, resource="installation"),
    )
    assert resp.status_code == 202, resp.text
    assert resp.json() == {"accepted": True, "skipped": True}
    assert await _trigger_events(sf, workspace_id) == []
