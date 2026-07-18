"""/api/v1/connectors — founder-facing connector_account CRUD (Workflow §11.2).

Proves the founder can register / list / revoke a connector_account and learn
the webhook URL to paste into the external service, and that the CRUD wires up
to the EXISTING public webhook ingress:

* create returns the ``webhook_token`` + full webhook URL ONCE (capability,
  like an API key);
* list never leaks the signing secret / its ciphertext and never returns the
  full token (only a masked hint);
* create → a signed Slack webhook to the returned URL lands a TriggerEvent
  (the CRUD + ingress connect);
* revoke → that same ingress 404s (soft-revoke flips ``is_active``);
* workspace isolation — workspace B cannot see / revoke A's connector;
* validation rejects an unknown connector + extra fields + an empty secret.

Auth + workspace are injected via dependency overrides (the v1 router shape),
exactly like ``tests/api/test_v1_workspaces_products.py``. The public ingress
needs no auth; its credential cipher is overridden onto the same deterministic
key so a created account's encrypted secret round-trips. Runs on in-memory
SQLite by default, real Postgres when ``BSVIBE_DATABASE_URL`` is set.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.api.deps import get_current_user, get_db_session, get_workspace_id
from backend.api.main import create_app
from backend.api.webhooks import get_credential_cipher
from backend.connectors.db import ConnectorAccountRow
from backend.router.accounts.crypto import CredentialCipher
from backend.workflow.infrastructure.intake.db import TriggerEventRow  # noqa: F401 — register table

from .._support import db_engine, fake_current_user

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


def _make_client(
    app, sf: async_sessionmaker[AsyncSession], cipher: CredentialCipher, ws: uuid.UUID
) -> httpx.AsyncClient:
    async def _session():
        async with sf() as s:
            yield s

    app.dependency_overrides[get_current_user] = fake_current_user()
    app.dependency_overrides[get_workspace_id] = lambda: ws
    app.dependency_overrides[get_db_session] = _session
    app.dependency_overrides[get_credential_cipher] = lambda: cipher
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


@pytest_asyncio.fixture
async def client(sf, cipher: CredentialCipher, workspace_id: uuid.UUID):
    app = create_app()
    async with _make_client(app, sf, cipher, workspace_id) as c:
        yield c


# --------------------------------------------------------------------------
# Slack signing helper (mirrors tests/glue/test_webhook_inbound_e2e.py)
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
# create
# --------------------------------------------------------------------------


async def test_create_returns_token_and_full_url_once(client: httpx.AsyncClient) -> None:
    r = await client.post(
        "/api/v1/connectors",
        json={"connector": "slack", "signing_secret": "shhh", "external_ref": "T1/team"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["connector"] == "slack"
    assert body["external_ref"] == "T1/team"
    assert body["is_active"] is True
    # The capability is shown exactly once.
    token = body["webhook_token"]
    assert token and len(token) >= 32
    assert body["webhook_url"] == f"/api/webhooks/slack/{token}"
    # Never the secret.
    assert "signing_secret" not in body
    assert "signing_secret_ciphertext" not in body


async def test_create_accepts_and_echoes_delivery_config(client: httpx.AsyncClient) -> None:
    r = await client.post(
        "/api/v1/connectors",
        json={
            "connector": "notion",
            "signing_secret": "secret_notion_token",
            "delivery_config": {"parent_page_id": "P"},
        },
    )
    assert r.status_code == 201, r.text
    assert r.json()["delivery_config"] == {"parent_page_id": "P"}

    listed = await client.get("/api/v1/connectors")
    assert listed.status_code == 200
    assert listed.json()[0]["delivery_config"] == {"parent_page_id": "P"}


async def test_create_defaults_delivery_config_to_empty(client: httpx.AsyncClient) -> None:
    r = await client.post(
        "/api/v1/connectors",
        json={"connector": "slack", "signing_secret": "x"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["delivery_config"] == {}


async def test_list_surfaces_oauth_account_label_when_connected(
    client: httpx.AsyncClient,
    sf: async_sessionmaker[AsyncSession],
    cipher: CredentialCipher,
    workspace_id: uuid.UUID,
) -> None:
    """A github binding with an OAuth token row reports its @login to the UI."""
    from backend.connectors.auth import store  # noqa: PLC0415
    from backend.connectors.auth.tokenset import TokenSet  # noqa: PLC0415

    r = await client.post(
        "/api/v1/connectors",
        json={"connector": "github", "signing_secret": "no-webhook-secret"},
    )
    assert r.status_code == 201, r.text
    account_id = uuid.UUID(r.json()["id"])

    async with sf() as s:
        await store.upsert_token(
            s,
            connector_account_id=account_id,
            provider="github",
            token=TokenSet(access_token="ghu_x", account_label="@octocat"),
            cipher=cipher,
        )
        await s.commit()

    listed = await client.get("/api/v1/connectors")
    assert listed.status_code == 200
    row = next(x for x in listed.json() if x["id"] == str(account_id))
    assert row["oauth_account_label"] == "@octocat"


async def test_list_oauth_account_label_none_when_not_connected(
    client: httpx.AsyncClient,
) -> None:
    """A connector with no OAuth token row reports a null label (not connected)."""
    r = await client.post(
        "/api/v1/connectors",
        json={"connector": "slack", "signing_secret": "x"},
    )
    assert r.status_code == 201, r.text
    listed = await client.get("/api/v1/connectors")
    assert listed.json()[0]["oauth_account_label"] is None


async def test_create_rejects_unknown_connector(client: httpx.AsyncClient) -> None:
    r = await client.post(
        "/api/v1/connectors",
        json={"connector": "pager-duty", "signing_secret": "x"},
    )
    assert r.status_code == 422, r.text


async def test_create_rejects_empty_secret(client: httpx.AsyncClient) -> None:
    r = await client.post(
        "/api/v1/connectors",
        json={"connector": "slack", "signing_secret": ""},
    )
    assert r.status_code == 422, r.text


async def test_create_rejects_extra_fields(client: httpx.AsyncClient) -> None:
    r = await client.post(
        "/api/v1/connectors",
        json={"connector": "slack", "signing_secret": "x", "is_active": False},
    )
    assert r.status_code == 422, r.text


@pytest.mark.parametrize("suppressed", ["linear", "trello"])
async def test_create_rejects_suppressed_connectors(
    client: httpx.AsyncClient, suppressed: str
) -> None:
    """INV-1 — linear/trello build outbound but are NOT user-connectable.

    Their outbound builders keep delivering existing bindings, but they are a
    product-suppression decision — the create front door rejects them as if
    unknown (422), same shape as a genuinely unknown connector.
    """
    r = await client.post(
        "/api/v1/connectors",
        json={"connector": suppressed, "signing_secret": "x"},
    )
    assert r.status_code == 422, r.text


async def test_create_still_accepts_connectable(client: httpx.AsyncClient) -> None:
    """A user-connectable connector (slack) is still creatable after the cutover."""
    r = await client.post(
        "/api/v1/connectors",
        json={"connector": "slack", "signing_secret": "x"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["outbound"] is True
    assert body["webhook_trigger"] is True


# --------------------------------------------------------------------------
# catalog
# --------------------------------------------------------------------------


async def test_catalog_lists_user_connectable_with_flags(client: httpx.AsyncClient) -> None:
    """GET /connectors/catalog returns the founder-visible catalog with flags."""
    r = await client.get("/api/v1/connectors/catalog")
    assert r.status_code == 200, r.text
    entries = r.json()["connectors"]
    by_name = {e["name"]: e for e in entries}

    # Suppressed connectors are naturally absent.
    assert "linear" not in by_name
    assert "trello" not in by_name

    # Representative capability shapes.
    assert by_name["slack"]["outbound"] is True
    assert by_name["slack"]["webhook_trigger"] is True
    assert by_name["slack"]["importable"] is False

    assert by_name["obsidian"]["importable"] is True
    assert by_name["obsidian"]["import_action"] == "import_vault"
    assert by_name["obsidian"]["outbound"] is False

    assert by_name["notion"]["outbound"] is True
    assert by_name["notion"]["importable"] is True
    assert by_name["notion"]["artifact_types"] == ["page", "page_image"]

    # Every entry carries the full flag set (extra=forbid enforces exactness).
    for e in entries:
        assert set(e) == {
            "name",
            "outbound",
            "importable",
            "webhook_trigger",
            "artifact_types",
            "import_action",
        }


# --------------------------------------------------------------------------
# list
# --------------------------------------------------------------------------


async def test_list_never_leaks_secret_or_full_token(client: httpx.AsyncClient) -> None:
    create = await client.post(
        "/api/v1/connectors",
        json={"connector": "github", "signing_secret": "top-secret-value"},
    )
    assert create.status_code == 201
    full_token = create.json()["webhook_token"]

    r = await client.get("/api/v1/connectors")
    assert r.status_code == 200, r.text
    items = r.json()
    assert len(items) == 1
    item = items[0]
    # Never the secret, never its ciphertext, never the full token.
    serialized = json.dumps(item)
    assert "top-secret-value" not in serialized
    assert "signing_secret" not in item
    assert "signing_secret_ciphertext" not in item
    assert "webhook_token" not in item
    assert "webhook_url" not in item
    # A masked hint only (last 4 chars of the token).
    assert item["token_hint"].endswith(full_token[-4:])
    assert full_token not in serialized
    assert item["connector"] == "github"
    assert item["is_active"] is True
    assert "id" in item and "created_at" in item


# --------------------------------------------------------------------------
# create → ingress round-trip + revoke
# --------------------------------------------------------------------------


async def test_create_then_signed_webhook_lands_trigger_event(
    client: httpx.AsyncClient,
    sf: async_sessionmaker[AsyncSession],
    workspace_id: uuid.UUID,
) -> None:
    secret = "shhh-signing-secret"
    create = await client.post(
        "/api/v1/connectors",
        json={"connector": "slack", "signing_secret": secret},
    )
    assert create.status_code == 201, create.text
    url = create.json()["webhook_url"]

    body = _slack_event_body()
    resp = await client.post(url, content=body, headers=_slack_headers(secret, body))
    assert resp.status_code == 202, resp.text
    assert resp.json() == {"accepted": True, "duplicate": False}

    events = await _trigger_events(sf, workspace_id)
    assert len(events) == 1
    assert events[0].source == "slack"
    assert events[0].workspace_id == workspace_id


async def test_revoke_makes_ingress_404(
    client: httpx.AsyncClient,
    sf: async_sessionmaker[AsyncSession],
    workspace_id: uuid.UUID,
) -> None:
    secret = "shhh-signing-secret"
    create = await client.post(
        "/api/v1/connectors",
        json={"connector": "slack", "signing_secret": secret},
    )
    assert create.status_code == 201, create.text
    connector_id = create.json()["id"]
    url = create.json()["webhook_url"]

    revoke = await client.delete(f"/api/v1/connectors/{connector_id}")
    assert revoke.status_code == 204, revoke.text

    # The row is soft-revoked: still present but is_active=False.
    async with sf() as s:
        row = await s.get(ConnectorAccountRow, uuid.UUID(connector_id))
        assert row is not None
        assert row.is_active is False

    # The ingress now resolves nothing → 404.
    body = _slack_event_body()
    resp = await client.post(url, content=body, headers=_slack_headers(secret, body))
    assert resp.status_code == 404, resp.text
    assert await _trigger_events(sf, workspace_id) == []


async def test_revoke_unknown_id_is_404(client: httpx.AsyncClient) -> None:
    r = await client.delete(f"/api/v1/connectors/{uuid.uuid4()}")
    assert r.status_code == 404, r.text


# --------------------------------------------------------------------------
# workspace isolation
# --------------------------------------------------------------------------


async def test_workspace_isolation_list_and_revoke(
    sf: async_sessionmaker[AsyncSession], cipher: CredentialCipher
) -> None:
    ws_a = uuid.uuid4()
    ws_b = uuid.uuid4()
    app = create_app()

    # Workspace A creates a connector.
    async with _make_client(app, sf, cipher, ws_a) as ca:
        created = await ca.post(
            "/api/v1/connectors",
            json={"connector": "slack", "signing_secret": "a-secret"},
        )
        assert created.status_code == 201
        connector_id = created.json()["id"]

    # Workspace B cannot see it or revoke it.
    app_b = create_app()
    async with _make_client(app_b, sf, cipher, ws_b) as cb:
        listing = await cb.get("/api/v1/connectors")
        assert listing.status_code == 200
        assert listing.json() == []

        revoke = await cb.delete(f"/api/v1/connectors/{connector_id}")
        assert revoke.status_code == 404, revoke.text

    # The connector is still active for A.
    async with sf() as s:
        row = await s.get(ConnectorAccountRow, uuid.UUID(connector_id))
        assert row is not None
        assert row.is_active is True
