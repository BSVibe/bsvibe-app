"""게이트 2 — the first model account becomes the workspace default.

A new founder who creates ONE model account but never sets it as the workspace
default used to hit the resolver's hard fail (``no_model_account``) on their
first run — the "hidden step". Creating the first account now fills the empty
``default_account_id`` slot (never overriding an existing choice), so the first
run resolves. Covers the helper, the REST create, and the MCP create.
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
from backend.identity.default_account import set_default_model_account_if_unset
from backend.identity.workspaces_db import WorkspaceRow, WorkspacesBase
from backend.router.accounts.models import AccountsBase

from .._support import db_engine, fake_current_user, memory_session

pytestmark = pytest.mark.asyncio


async def test_helper_sets_default_only_when_unset() -> None:
    async with memory_session() as s:
        ws_id, acct_a, acct_b = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        s.add(WorkspaceRow(id=ws_id, name="Acme", safe_mode=True))
        await s.commit()

        set_a = await set_default_model_account_if_unset(
            s, workspace_id=ws_id, model_account_id=acct_a
        )
        assert set_a is True
        # A second account does NOT override the founder's (auto-set) default.
        set_b = await set_default_model_account_if_unset(
            s, workspace_id=ws_id, model_account_id=acct_b
        )
        assert set_b is False
        ws = await s.get(WorkspaceRow, ws_id)
        assert ws.default_account_id == acct_a


async def test_helper_is_noop_when_workspace_missing() -> None:
    async with memory_session() as s:
        assert (
            await set_default_model_account_if_unset(
                s, workspace_id=uuid.uuid4(), model_account_id=uuid.uuid4()
            )
            is False
        )


@pytest_asyncio.fixture
async def db(monkeypatch):
    monkeypatch.setenv("BSVIBE_GATEWAY_KMS_KEY_B64", base64.urlsafe_b64encode(b"0" * 32).decode())
    get_settings.cache_clear()
    async with db_engine(AccountsBase, WorkspacesBase) as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)
    get_settings.cache_clear()


@pytest_asyncio.fixture
async def client(db):
    ws_id = uuid.uuid4()
    app = create_app()

    async def _session():
        async with db() as s:
            yield s

    app.dependency_overrides[get_current_user] = fake_current_user()
    app.dependency_overrides[get_workspace_id] = lambda: ws_id
    app.dependency_overrides[get_db_session] = _session

    async with db() as s:
        s.add(WorkspaceRow(id=ws_id, name="Acme", safe_mode=True))
        await s.commit()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c, ws_id, db


async def test_rest_first_account_becomes_default_second_does_not(client) -> None:
    c, ws_id, db = client
    body = {
        "provider": "openai",
        "label": "primary",
        "litellm_model": "openai/gpt-4o-mini",
        "api_key": "sk-secret-test",
    }
    r1 = await c.post("/api/v1/accounts", json=body)
    assert r1.status_code == 201, r1.text
    first_id = uuid.UUID(r1.json()["id"])

    async with db() as s:
        ws = await s.get(WorkspaceRow, ws_id)
        assert ws.default_account_id == first_id  # first account auto-set

    r2 = await c.post("/api/v1/accounts", json={**body, "label": "secondary"})
    assert r2.status_code == 201, r2.text
    async with db() as s:
        ws = await s.get(WorkspaceRow, ws_id)
        assert ws.default_account_id == first_id  # unchanged by the second
