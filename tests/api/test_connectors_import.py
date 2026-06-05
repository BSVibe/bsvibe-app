"""/api/v1/connectors/{id}/import — Lift B inbound bulk-import trigger.

Proves the founder UI's "Import now" button has a real backend:

* happy path for each inbound connector (obsidian / claude / gpt / notion)
  — dispatcher gets the bound ``delivery_config`` injected into
  :class:`SkillContext.config` and its return value is surfaced as
  ``detail`` while ``imported_count`` is normalised + persisted on the row;
* 404 — connector id not present in this workspace (workspace isolation
  reuses the same path);
* 404 — soft-revoked binding looks the same as missing to the import
  surface, mirroring the public ingress;
* 422 — outbound-only connector (github) rejects with a clear reason;
* 422 — connector whose inbound is push-only (slack) rejects with a clear
  reason — the kind is ``both`` but
  :data:`backend.connectors.kinds.INBOUND_IMPORT_ACTIONS` has no entry,
  so the route 422s before reaching the dispatcher.

Auth + workspace + cipher are injected via dependency overrides exactly
like ``tests/api/test_connectors.py``. The :class:`ImportDispatcher` is a
test fake so the suite never touches the plugin loader / filesystem /
KMS — that wiring is exercised by the production
:func:`get_import_dispatcher` factory at runtime.
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
from backend.api.v1.connectors import get_import_dispatcher
from backend.api.webhooks import get_credential_cipher
from backend.connectors.db import ConnectorAccountRow
from backend.router.accounts.crypto import CredentialCipher

from .._support import db_engine, fake_current_user

# Deterministic 32-byte AES key for CredentialCipher in tests.
TEST_KEY = b"0123456789abcdef0123456789abcdef"

pytestmark = pytest.mark.asyncio


# ── fixtures ───────────────────────────────────────────────────────────────


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


class FakeDispatcher:
    """In-test :class:`ImportDispatcher` capturing call args.

    Records every ``(row, workspace_id)`` it was called with so the test
    can assert the route resolved the right binding + passed its
    ``delivery_config`` through. Returns the canned ``result``.
    """

    def __init__(self, result: dict[str, Any] | Exception) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    async def import_for(
        self,
        *,
        row: ConnectorAccountRow,
        workspace_id: uuid.UUID,
        session: Any = None,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "connector": row.connector,
                "row_id": row.id,
                "workspace_id": workspace_id,
                "delivery_config": dict(row.delivery_config or {}),
            }
        )
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _make_client(
    app,
    sf: async_sessionmaker[AsyncSession],
    cipher: CredentialCipher,
    ws: uuid.UUID,
    dispatcher: Any | None = None,
) -> httpx.AsyncClient:
    async def _session():
        async with sf() as s:
            yield s

    app.dependency_overrides[get_current_user] = fake_current_user()
    app.dependency_overrides[get_workspace_id] = lambda: ws
    app.dependency_overrides[get_db_session] = _session
    app.dependency_overrides[get_credential_cipher] = lambda: cipher
    if dispatcher is not None:
        app.dependency_overrides[get_import_dispatcher] = lambda: dispatcher
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def _create(client: httpx.AsyncClient, connector: str, **extra: Any) -> dict[str, Any]:
    payload = {"connector": connector, "signing_secret": "secret", **extra}
    r = await client.post("/api/v1/connectors", json=payload)
    assert r.status_code == 201, r.text
    return r.json()


# ── happy path per inbound connector ──────────────────────────────────────


@pytest.mark.parametrize(
    "connector,detail,expected_count",
    [
        (
            "obsidian",
            {"notes_count": 7, "scanned_count": 9, "skipped_count": 2, "region": "imported"},
            7,
        ),
        (
            "claude",
            {
                "conversations_count": 3,
                "messages_count": 24,
                "skipped": 0,
                "region": "imported-claude",
            },
            3,
        ),
        (
            "gpt",
            {
                "conversations_count": 5,
                "messages_count": 41,
                "skipped": 1,
                "region": "imported-gpt",
            },
            5,
        ),
        (
            "notion",
            {"pages_count": 11, "blocks_count": 88, "skipped": 0, "region": "imported-notion"},
            11,
        ),
    ],
)
async def test_import_happy_path(
    sf: async_sessionmaker[AsyncSession],
    cipher: CredentialCipher,
    workspace_id: uuid.UUID,
    connector: str,
    detail: dict[str, Any],
    expected_count: int,
) -> None:
    dispatcher = FakeDispatcher(detail)
    app = create_app()
    async with _make_client(app, sf, cipher, workspace_id, dispatcher) as c:
        delivery_config = {"vault_path": "/tmp/vault"} if connector == "obsidian" else {"x": "y"}
        created = await _create(c, connector, delivery_config=delivery_config)
        connector_id = created["id"]
        # The create response includes the new ``kind`` field — wired through
        # for both inbound-only (obsidian) and "both" (notion) connectors.
        assert created["kind"] in ("inbound", "both")

        r = await c.post(f"/api/v1/connectors/{connector_id}/import", json={})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["imported_count"] == expected_count
        assert body["detail"] == detail
        assert body["last_import_at"]

        # The dispatcher saw the bound ``delivery_config`` exactly.
        assert len(dispatcher.calls) == 1
        call = dispatcher.calls[0]
        assert call["connector"] == connector
        assert call["workspace_id"] == workspace_id
        assert call["delivery_config"] == delivery_config

        # The list response now reflects the new ``last_import_*`` columns.
        listed = await c.get("/api/v1/connectors")
        assert listed.status_code == 200
        item = listed.json()[0]
        assert item["last_import_count"] == expected_count
        assert item["last_import_at"]
        assert item["kind"] in ("inbound", "both")


# ── 404 / 422 / 502 paths ─────────────────────────────────────────────────


async def test_import_unknown_id_is_404(
    sf: async_sessionmaker[AsyncSession],
    cipher: CredentialCipher,
    workspace_id: uuid.UUID,
) -> None:
    app = create_app()
    async with _make_client(app, sf, cipher, workspace_id, FakeDispatcher({})) as c:
        r = await c.post(f"/api/v1/connectors/{uuid.uuid4()}/import", json={})
        assert r.status_code == 404, r.text


async def test_import_revoked_is_404(
    sf: async_sessionmaker[AsyncSession],
    cipher: CredentialCipher,
    workspace_id: uuid.UUID,
) -> None:
    dispatcher = FakeDispatcher({"notes_count": 1})
    app = create_app()
    async with _make_client(app, sf, cipher, workspace_id, dispatcher) as c:
        created = await _create(c, "obsidian", delivery_config={"vault_path": "/tmp/v"})
        connector_id = created["id"]
        revoke = await c.delete(f"/api/v1/connectors/{connector_id}")
        assert revoke.status_code == 204

        r = await c.post(f"/api/v1/connectors/{connector_id}/import", json={})
        assert r.status_code == 404, r.text
        # Dispatcher must not have been touched for a revoked binding.
        assert dispatcher.calls == []


async def test_import_rejects_outbound_only(
    sf: async_sessionmaker[AsyncSession],
    cipher: CredentialCipher,
    workspace_id: uuid.UUID,
) -> None:
    dispatcher = FakeDispatcher({})
    app = create_app()
    async with _make_client(app, sf, cipher, workspace_id, dispatcher) as c:
        created = await _create(c, "github")
        # The kind surfaces as "outbound" already in the create response.
        assert created["kind"] == "outbound"
        r = await c.post(f"/api/v1/connectors/{created['id']}/import", json={})
        assert r.status_code == 422, r.text
        assert "outbound-only" in r.json()["detail"]
        assert dispatcher.calls == []


async def test_import_rejects_push_only_inbound(
    sf: async_sessionmaker[AsyncSession],
    cipher: CredentialCipher,
    workspace_id: uuid.UUID,
) -> None:
    """Slack is kind="both" but its inbound is webhook-driven (no bulk import).

    The route 422s with a clear "no bulk-import action" message rather than
    silently 200ing on a no-op dispatch.
    """
    dispatcher = FakeDispatcher({})
    app = create_app()
    async with _make_client(app, sf, cipher, workspace_id, dispatcher) as c:
        created = await _create(c, "slack")
        r = await c.post(f"/api/v1/connectors/{created['id']}/import", json={})
        assert r.status_code == 422, r.text
        assert "no bulk-import" in r.json()["detail"]
        assert dispatcher.calls == []


async def test_import_plugin_run_error_is_502(
    sf: async_sessionmaker[AsyncSession],
    cipher: CredentialCipher,
    workspace_id: uuid.UUID,
) -> None:
    from backend.extensions.plugin.base import PluginRunError

    dispatcher = FakeDispatcher(PluginRunError("vault not found"))
    app = create_app()
    async with _make_client(app, sf, cipher, workspace_id, dispatcher) as c:
        created = await _create(c, "obsidian", delivery_config={"vault_path": "/nope"})
        r = await c.post(f"/api/v1/connectors/{created['id']}/import", json={})
        assert r.status_code == 502, r.text
        assert "vault not found" in r.json()["detail"]


# ── new known-connector validator ─────────────────────────────────────────


@pytest.mark.parametrize("name", ["obsidian", "claude", "gpt"])
async def test_create_accepts_new_inbound_connectors(
    sf: async_sessionmaker[AsyncSession],
    cipher: CredentialCipher,
    workspace_id: uuid.UUID,
    name: str,
) -> None:
    """Lift B widens the validator to accept the inbound-only connectors.

    These have NEITHER an inbound webhook parser NOR an outbound delivery
    builder — they were unknown to the legacy validator. The kind map now
    declares them, so the create succeeds.
    """
    app = create_app()
    async with _make_client(app, sf, cipher, workspace_id) as c:
        r = await c.post(
            "/api/v1/connectors",
            json={"connector": name, "signing_secret": "x"},
        )
        assert r.status_code == 201, r.text
        assert r.json()["kind"] == "inbound"


# ── workspace isolation for import ────────────────────────────────────────


async def test_import_workspace_isolation(
    sf: async_sessionmaker[AsyncSession], cipher: CredentialCipher
) -> None:
    """Workspace B cannot import workspace A's connector."""
    ws_a = uuid.uuid4()
    ws_b = uuid.uuid4()
    dispatcher_a = FakeDispatcher({"notes_count": 4})
    dispatcher_b = FakeDispatcher({"notes_count": 4})

    app = create_app()
    async with _make_client(app, sf, cipher, ws_a, dispatcher_a) as ca:
        created = await _create(ca, "obsidian", delivery_config={"vault_path": "/v"})
        connector_id = created["id"]

    app_b = create_app()
    async with _make_client(app_b, sf, cipher, ws_b, dispatcher_b) as cb:
        r = await cb.post(f"/api/v1/connectors/{connector_id}/import", json={})
        assert r.status_code == 404, r.text
        assert dispatcher_b.calls == []
