"""/api/v1/accounts — end-to-end against real Postgres.

The ``/api/v1/decisions`` queue surface used to be DB-sourced and was tested
here too, but the list now reads the workspace vault (FS-as-SoT) — the same
store accept/reject resolve against — so those scenarios live alongside the
resolution tests in ``test_decisions_resolve.py``.
"""

from __future__ import annotations

import base64
import uuid

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.api.deps import (
    get_current_user,
    get_db_session,
    get_workspace_id,
    require_account_id,
)
from backend.api.main import create_app
from backend.config import get_settings
from backend.knowledge.canonicalization.db import CanonicalizationBase
from backend.router.accounts.models import AccountsBase

from .._support import db_engine, fake_current_user

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def db(monkeypatch):
    # Provide a deterministic KMS key for the cipher init.
    monkeypatch.setenv("BSVIBE_GATEWAY_KMS_KEY_B64", base64.urlsafe_b64encode(b"0" * 32).decode())
    get_settings.cache_clear()
    async with db_engine(AccountsBase, CanonicalizationBase) as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)
    get_settings.cache_clear()


@pytest.fixture
def workspace_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def account_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest_asyncio.fixture
async def client(db, workspace_id, account_id):
    app = create_app()

    def _ws() -> uuid.UUID:
        return workspace_id

    def _acct() -> uuid.UUID:
        return account_id

    async def _session():
        async with db() as s:
            yield s

    app.dependency_overrides[get_current_user] = fake_current_user()
    app.dependency_overrides[get_workspace_id] = _ws
    app.dependency_overrides[require_account_id] = _acct
    app.dependency_overrides[get_db_session] = _session

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_accounts_full_crud(client) -> None:
    # Empty list
    r = await client.get("/api/v1/accounts")
    assert r.status_code == 200
    assert r.json() == []

    # Create
    create_body = {
        "provider": "openai",
        "label": "primary",
        "litellm_model": "openai/gpt-4o-mini",
        "api_key": "sk-secret-test",
        "data_jurisdiction": "us",
    }
    r = await client.post("/api/v1/accounts", json=create_body)
    assert r.status_code == 201, r.text
    created = r.json()
    ma_id = created["id"]
    assert created["label"] == "primary"
    # Encrypted; never echoes the api_key
    assert "api_key" not in created

    # List → 1 row
    r = await client.get("/api/v1/accounts")
    assert r.status_code == 200
    assert len(r.json()) == 1

    # Get
    r = await client.get(f"/api/v1/accounts/{ma_id}")
    assert r.status_code == 200
    assert r.json()["id"] == ma_id

    # Patch
    r = await client.patch(f"/api/v1/accounts/{ma_id}", json={"label": "renamed"})
    assert r.status_code == 200
    assert r.json()["label"] == "renamed"

    # Delete
    r = await client.delete(f"/api/v1/accounts/{ma_id}")
    assert r.status_code == 204
    r = await client.get(f"/api/v1/accounts/{ma_id}")
    assert r.status_code == 404
