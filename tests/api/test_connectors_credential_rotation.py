"""``PUT /api/v1/connectors/{id}/credential`` — swap a token, keep the row.

A token-connected connector used to have no way to change its token: the only
road was revoke + re-create, which mints a NEW row and so strands everything
that pointed at the old one — the product bindings (``resource_bindings``
cascade-delete with the account), the approver allowlist, ``chat_id`` and the
webhook URL. The route rotates ONLY the credential; these tests pin what must
survive and what must never leak.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest
import pytest_asyncio
import structlog
import structlog.testing
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.api.deps import get_current_user, get_db_session, get_workspace_id
from backend.api.main import create_app
from backend.api.v1.connectors import get_telegram_client_factory
from backend.api.webhooks import get_credential_cipher
from backend.connectors.db import ConnectorAccountRow
from backend.identity.workspaces_db import ProductRow, ResourceBindingRow, WorkspaceRow
from backend.router.accounts.crypto import CredentialCipher

from .._support import db_engine, fake_current_user, publishing_workspace
from ..mcp._rls import scoped_session

TEST_KEY = b"0123456789abcdef0123456789abcdef"
OLD_TOKEN = "111111:old-bot-token-AAAA"
NEW_TOKEN = "222222:new-bot-token-BBBB"

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
    """One factory for the whole test: records which token every call used."""

    def __init__(self, *, rejects: set[str] | None = None, webhook_fails: bool = False) -> None:
        self.rejects = rejects or set()
        self.webhook_fails = webhook_fails
        self.get_me_tokens: list[str] = []
        self.webhook_tokens: list[str] = []
        self._url = ""

    def __call__(self, token: str) -> Any:
        outer = self

        class _Client:
            async def get_me(self) -> dict[str, Any]:
                outer.get_me_tokens.append(token)
                if token in outer.rejects:
                    # A real client's text would name the token-bearing URL.
                    raise RuntimeError(f"401 Unauthorized for url '/bot{token}/getMe'")
                return {"id": 1, "is_bot": True}

            async def set_webhook(self, url: str, *, secret_token: str) -> dict[str, Any]:
                outer.webhook_tokens.append(token)
                if outer.webhook_fails:
                    raise RuntimeError("telegram is down")
                outer._url = url
                return {"ok": True}

            async def get_webhook_info(self) -> dict[str, Any]:
                return {"url": outer._url, "pending_update_count": 0}

        return _Client()


def _make_client(
    app, sf: async_sessionmaker[AsyncSession], cipher: CredentialCipher, ws: uuid.UUID, tg: Any
) -> httpx.AsyncClient:
    async def _session():
        async with sf() as s:
            yield s

    app.dependency_overrides[get_current_user] = fake_current_user()
    app.dependency_overrides[get_workspace_id] = publishing_workspace(ws)
    app.dependency_overrides[get_db_session] = _session
    app.dependency_overrides[get_credential_cipher] = lambda: cipher
    app.dependency_overrides[get_telegram_client_factory] = lambda: tg
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _create(client: httpx.AsyncClient, connector: str, **extra: Any) -> dict[str, Any]:
    r = await client.post(
        "/api/v1/connectors", json={"connector": connector, "signing_secret": OLD_TOKEN, **extra}
    )
    assert r.status_code == 201, r.text
    return r.json()


async def _bind(sf, ws: uuid.UUID, account_id: str) -> None:
    async with scoped_session(sf, ws) as s:
        if await s.get(WorkspaceRow, ws) is None:
            s.add(WorkspaceRow(id=ws, name="ws", safe_mode=False))
            await s.flush()
        product_id = uuid.uuid4()
        s.add(ProductRow(id=product_id, workspace_id=ws, name="p", slug=uuid.uuid4().hex[:12]))
        await s.flush()
        s.add(
            ResourceBindingRow(
                id=uuid.uuid4(),
                workspace_id=ws,
                product_id=product_id,
                connector_account_id=uuid.UUID(account_id),
                resource_id="r1",
            )
        )
        await s.commit()


async def _row(sf, connector_id: str) -> ConnectorAccountRow:
    async with sf() as s:
        row = await s.get(ConnectorAccountRow, uuid.UUID(connector_id))
        assert row is not None
        return row


async def test_rotation_keeps_the_row_its_bindings_and_its_config(sf, cipher, workspace_id):
    """R6 — same id, same bindings, same allowlist / chat_id, same webhook token."""
    tg = _FakeTelegram()
    config = {"chat_id": "42", "authorized_user_ids": ["u1", "u2"]}
    async with _make_client(create_app(), sf, cipher, workspace_id, tg) as client:
        created = await _create(client, "telegram", delivery_config=config)
        await _bind(sf, workspace_id, created["id"])
        before = await _row(sf, created["id"])

        r = await client.put(
            f"/api/v1/connectors/{created['id']}/credential",
            json={"signing_secret": NEW_TOKEN},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        listed = (await client.get("/api/v1/connectors")).json()

    assert body["connector"]["id"] == created["id"]
    assert [c["id"] for c in listed] == [created["id"]]
    assert listed[0]["binding_count"] == 1
    after = await _row(sf, created["id"])
    assert after.webhook_token == before.webhook_token
    assert after.delivery_config["chat_id"] == "42"
    assert after.delivery_config["authorized_user_ids"] == ["u1", "u2"]
    async with scoped_session(sf, workspace_id) as s:
        remaining = await s.scalar(
            select(func.count())
            .select_from(ResourceBindingRow)
            .where(ResourceBindingRow.connector_account_id == uuid.UUID(created["id"]))
        )
    assert remaining == 1


async def test_after_rotation_only_the_new_token_is_stored_and_used(sf, cipher, workspace_id):
    """R8 — the decrypted value is the new one; the ingress is re-registered with it (R2)."""
    tg = _FakeTelegram()
    async with _make_client(create_app(), sf, cipher, workspace_id, tg) as client:
        created = await _create(client, "telegram", delivery_config={"chat_id": "42"})
        tg.get_me_tokens.clear()
        tg.webhook_tokens.clear()

        r = await client.put(
            f"/api/v1/connectors/{created['id']}/credential",
            json={"signing_secret": NEW_TOKEN},
        )
        assert r.status_code == 200, r.text

    row = await _row(sf, created["id"])
    assert cipher.decrypt(row.signing_secret_ciphertext) == NEW_TOKEN
    assert tg.get_me_tokens == [NEW_TOKEN]
    assert tg.webhook_tokens == [NEW_TOKEN]
    assert r.json()["webhook_registered"] is True


async def test_a_bad_new_token_never_overwrites_a_working_one(sf, cipher, workspace_id):
    """R3 — getMe is probed BEFORE the write; a typo leaves the connection alone."""
    tg = _FakeTelegram(rejects={NEW_TOKEN})
    async with _make_client(create_app(), sf, cipher, workspace_id, tg) as client:
        created = await _create(client, "telegram", delivery_config={"chat_id": "42"})
        tg.webhook_tokens.clear()
        r = await client.put(
            f"/api/v1/connectors/{created['id']}/credential",
            json={"signing_secret": NEW_TOKEN},
        )

    assert r.status_code == 422, r.text
    assert NEW_TOKEN not in r.text
    row = await _row(sf, created["id"])
    assert cipher.decrypt(row.signing_secret_ciphertext) == OLD_TOKEN
    assert tg.webhook_tokens == []


async def test_a_failed_reregistration_keeps_the_validated_token(sf, cipher, workspace_id):
    tg = _FakeTelegram()
    async with _make_client(create_app(), sf, cipher, workspace_id, tg) as client:
        created = await _create(client, "telegram", delivery_config={"chat_id": "42"})
        tg.webhook_fails = True
        r = await client.put(
            f"/api/v1/connectors/{created['id']}/credential",
            json={"signing_secret": NEW_TOKEN},
        )

    assert r.status_code == 200, r.text
    assert r.json()["webhook_registered"] is False
    row = await _row(sf, created["id"])
    assert cipher.decrypt(row.signing_secret_ciphertext) == NEW_TOKEN


async def test_a_non_telegram_connector_rotates_without_a_provider_call(sf, cipher, workspace_id):
    tg = _FakeTelegram()
    async with _make_client(create_app(), sf, cipher, workspace_id, tg) as client:
        created = await _create(client, "slack")
        r = await client.put(
            f"/api/v1/connectors/{created['id']}/credential",
            json={"signing_secret": "xoxb-new"},
        )

    assert r.status_code == 200, r.text
    assert r.json()["webhook_registered"] is None
    assert tg.get_me_tokens == []
    row = await _row(sf, created["id"])
    assert cipher.decrypt(row.signing_secret_ciphertext) == "xoxb-new"


async def test_no_token_value_in_the_response_or_the_logs(sf, cipher, workspace_id):
    """R9 — ``token_hint`` is all a surface ever gets, success and failure alike."""
    tg = _FakeTelegram(rejects={"999999:typo-token-CCCC"})
    async with _make_client(create_app(), sf, cipher, workspace_id, tg) as client:
        created = await _create(client, "telegram", delivery_config={"chat_id": "42"})
        with structlog.testing.capture_logs() as logs:
            ok = await client.put(
                f"/api/v1/connectors/{created['id']}/credential",
                json={"signing_secret": NEW_TOKEN},
            )
            bad = await client.put(
                f"/api/v1/connectors/{created['id']}/credential",
                json={"signing_secret": "999999:typo-token-CCCC"},
            )
        listed = await client.get("/api/v1/connectors")

    assert ok.status_code == 200 and bad.status_code == 422
    blob = ok.text + bad.text + listed.text + repr(logs)
    for secret in (OLD_TOKEN, NEW_TOKEN, "999999:typo-token-CCCC"):
        assert secret not in blob


async def test_another_workspaces_connector_is_not_found(sf, cipher, workspace_id):
    """R10 — and the stranger's attempt changes nothing."""
    tg = _FakeTelegram()
    async with _make_client(create_app(), sf, cipher, workspace_id, tg) as client:
        created = await _create(client, "telegram", delivery_config={"chat_id": "42"})

    async with _make_client(create_app(), sf, cipher, uuid.uuid4(), tg) as stranger:
        r = await stranger.put(
            f"/api/v1/connectors/{created['id']}/credential",
            json={"signing_secret": NEW_TOKEN},
        )

    assert r.status_code == 404, r.text
    row = await _row(sf, created["id"])
    assert cipher.decrypt(row.signing_secret_ciphertext) == OLD_TOKEN


async def test_a_revoked_connector_is_not_found(sf, cipher, workspace_id):
    tg = _FakeTelegram()
    async with _make_client(create_app(), sf, cipher, workspace_id, tg) as client:
        created = await _create(client, "telegram", delivery_config={"chat_id": "42"})
        assert (await client.delete(f"/api/v1/connectors/{created['id']}")).status_code == 204
        r = await client.put(
            f"/api/v1/connectors/{created['id']}/credential",
            json={"signing_secret": NEW_TOKEN},
        )
    assert r.status_code == 404, r.text


async def test_the_list_shows_which_bindings_a_revoke_would_orphan(sf, cipher, workspace_id):
    """R7 — a connector with product bindings says so; one without says 0."""
    tg = _FakeTelegram()
    async with _make_client(create_app(), sf, cipher, workspace_id, tg) as client:
        bound = await _create(client, "telegram", delivery_config={"chat_id": "42"})
        free = await _create(client, "slack")
        await _bind(sf, workspace_id, bound["id"])
        listed = {c["id"]: c for c in (await client.get("/api/v1/connectors")).json()}

    assert listed[bound["id"]]["binding_count"] == 1
    assert listed[free["id"]]["binding_count"] == 0
