"""/api/v1/account (singular) discovery + the require_account_id fallback.

Distinct from ``test_v1_accounts_decisions.py`` which overrides
``require_account_id`` with a fixed id. Here we deliberately do NOT override it
so the production resolution runs:

* ``GET /api/v1/account`` create-on-reads the workspace's personal account.
* ``GET /api/v1/accounts`` (plural, model accounts) now returns 200 WITHOUT an
  ``X-BSVibe-Account-Id`` header — the server resolves the personal account.
* An explicit (valid) header still wins.
"""

from __future__ import annotations

import base64
import uuid

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.api.deps import get_current_user, get_db_session, get_workspace_id
from backend.api.main import create_app
from backend.config import get_settings
from backend.router.accounts.account_models import Account
from backend.router.accounts.models import AccountsBase

from .._support import db_engine, fake_current_user

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def db(monkeypatch):
    monkeypatch.setenv("BSVIBE_GATEWAY_KMS_KEY_B64", base64.urlsafe_b64encode(b"0" * 32).decode())
    get_settings.cache_clear()
    async with db_engine(AccountsBase) as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)
    get_settings.cache_clear()


@pytest.fixture
def workspace_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest_asyncio.fixture
async def client(db, workspace_id):
    """Client that overrides auth + workspace + session, but NOT the account
    axis — so ``require_account_id``'s real fallback runs."""
    app = create_app()

    def _ws() -> uuid.UUID:
        return workspace_id

    async def _session():
        async with db() as s:
            yield s

    app.dependency_overrides[get_current_user] = fake_current_user()
    app.dependency_overrides[get_workspace_id] = _ws
    app.dependency_overrides[get_db_session] = _session

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_get_account_returns_id_and_workspace(client, workspace_id) -> None:
    r = await client.get("/api/v1/account")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["workspace_id"] == str(workspace_id)
    assert uuid.UUID(body["id"])  # parseable uuid


async def test_get_account_is_stable_across_calls(client) -> None:
    r1 = await client.get("/api/v1/account")
    r2 = await client.get("/api/v1/account")
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json()["id"] == r2.json()["id"]


async def test_model_accounts_list_works_without_account_header(client) -> None:
    """The plural model-accounts route 200s with no X-BSVibe-Account-Id —
    the personal account is resolved server-side (the fallback)."""
    r = await client.get("/api/v1/accounts")
    assert r.status_code == 200, r.text
    assert r.json() == []


async def test_model_accounts_create_list_round_trip_via_fallback(client) -> None:
    create_body = {
        "provider": "openai",
        "label": "primary",
        "litellm_model": "openai/gpt-4o-mini",
        "api_key": "sk-secret-test",
        "data_jurisdiction": "us",
    }
    r = await client.post("/api/v1/accounts", json=create_body)
    assert r.status_code == 201, r.text
    # The created model account is partitioned under the resolved personal account.
    acct = await client.get("/api/v1/account")
    resolved_account_id = acct.json()["id"]
    assert r.json()["account_id"] == resolved_account_id

    r2 = await client.get("/api/v1/accounts")
    assert r2.status_code == 200
    assert len(r2.json()) == 1


async def test_create_without_jurisdiction_defaults_to_unknown(client) -> None:
    """The founder-facing form no longer sends ``data_jurisdiction``; a create
    body omitting it must 201 and the stored row defaults to ``unknown``."""
    create_body = {
        "provider": "openai",
        "label": "no-jurisdiction",
        "litellm_model": "openai/gpt-4o-mini",
        "api_key": "sk-secret-test",
    }
    r = await client.post("/api/v1/accounts", json=create_body)
    assert r.status_code == 201, r.text
    assert r.json()["data_jurisdiction"] == "unknown"


async def test_explicit_account_header_wins(client, db, workspace_id) -> None:
    """When a valid header is present it is used verbatim (orthogonal axis
    preserved), NOT replaced by the personal account."""
    explicit = uuid.uuid4()
    # Seed a model account under the explicit (non-personal) account id.
    create_body = {
        "provider": "openai",
        "label": "explicit",
        "litellm_model": "openai/gpt-4o-mini",
        "api_key": "sk-x",
        "data_jurisdiction": "us",
    }
    r = await client.post(
        "/api/v1/accounts",
        json=create_body,
        headers={"X-BSVibe-Account-Id": str(explicit)},
    )
    assert r.status_code == 201, r.text
    assert r.json()["account_id"] == str(explicit)

    # Listing WITH the explicit header sees it; listing WITHOUT (personal
    # fallback) does not.
    with_header = await client.get(
        "/api/v1/accounts", headers={"X-BSVibe-Account-Id": str(explicit)}
    )
    assert with_header.status_code == 200
    assert len(with_header.json()) == 1

    without_header = await client.get("/api/v1/accounts")
    assert without_header.status_code == 200
    assert without_header.json() == []


async def test_malformed_account_header_still_400s(client) -> None:
    r = await client.get("/api/v1/accounts", headers={"X-BSVibe-Account-Id": "not-a-uuid"})
    assert r.status_code == 400
    assert "invalid" in r.text.lower()


async def test_get_account_persists_single_row(client, db) -> None:
    await client.get("/api/v1/account")
    await client.get("/api/v1/account")
    async with db() as s:
        from sqlalchemy import select

        rows = (await s.execute(select(Account))).scalars().all()
        assert len(rows) == 1


async def test_model_accounts_list_includes_executor_rows(client, db) -> None:
    """Lift E7: provider=executor rows are first-class ModelAccounts now —
    they must surface in the Models list so the founder can set
    ``workspace.default_account_id`` and verify worker registration."""
    from backend.router.accounts.models import ModelAccount

    # Create a real LLM account through the API so the personal account exists.
    create_body = {
        "provider": "openai",
        "label": "primary",
        "litellm_model": "openai/gpt-4o-mini",
        "api_key": "sk-secret-test",
    }
    r = await client.post("/api/v1/accounts", json=create_body)
    assert r.status_code == 201, r.text
    account_id = uuid.UUID(r.json()["account_id"])
    ws = uuid.UUID(r.json()["workspace_id"])

    # Insert an executor row directly (the path register_worker uses).
    async with db() as s:
        s.add(
            ModelAccount(
                workspace_id=ws,
                account_id=account_id,
                provider="executor",
                label="laptop-1",
                litellm_model="executor/claude_code",
                api_base=None,
                api_key_encrypted=None,
                data_jurisdiction="unknown",
                extra_params={"worker_id": str(uuid.uuid4()), "executor_type": "claude_code"},
            )
        )
        await s.commit()

    listed = await client.get("/api/v1/accounts")
    assert listed.status_code == 200, listed.text
    providers = {row["provider"] for row in listed.json()}
    assert providers == {"openai", "executor"}
