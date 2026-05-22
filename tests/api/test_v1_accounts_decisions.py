"""/api/v1/{accounts,decisions} — end-to-end against real Postgres."""

from __future__ import annotations

import base64
import os
import uuid
from datetime import UTC, datetime

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.accounts.models import AccountsBase
from backend.api.deps import get_db_session, get_workspace_id, require_account_id
from backend.api.main import create_app
from backend.config import get_settings
from backend.knowledge.canonicalization.db import (
    ActionKind,
    CanonicalizationBase,
    CanonicalizationDecision,
    CanonicalizationProposal,
    DecisionKind,
    ProposalKind,
    ProposalStatus,
)

PG_URL = os.environ.get(
    "BSVIBE_DATABASE_URL", "postgresql+asyncpg://bsvibe:bsvibe@localhost:5442/bsvibe"
)


pytestmark = pytest.mark.asyncio


async def _can_reach_pg() -> bool:
    try:
        engine = create_async_engine(PG_URL, future=True, pool_pre_ping=True)
        async with engine.connect() as conn:
            await conn.execute(__import__("sqlalchemy").text("SELECT 1"))
        await engine.dispose()
        return True
    except Exception:
        return False


@pytest_asyncio.fixture
async def db(monkeypatch):
    # Provide a deterministic KMS key for the cipher init.
    monkeypatch.setenv("BSVIBE_GATEWAY_KMS_KEY_B64", base64.urlsafe_b64encode(b"0" * 32).decode())
    get_settings.cache_clear()
    use_pg = os.environ.get("BSVIBE_DATABASE_URL") and await _can_reach_pg()
    url = PG_URL if use_pg else "sqlite+aiosqlite:///:memory:"
    engine = create_async_engine(url, future=True)
    async with engine.begin() as conn:
        for base in (AccountsBase, CanonicalizationBase):
            await conn.run_sync(base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    yield sm
    if use_pg:
        async with engine.begin() as conn:
            for base in (CanonicalizationBase, AccountsBase):
                await conn.run_sync(base.metadata.drop_all)
    await engine.dispose()
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


async def test_decisions_proposals_list(client, db, workspace_id) -> None:
    async with db() as s:
        s.add(
            CanonicalizationProposal(
                id=uuid.uuid4(),
                workspace_id=workspace_id,
                proposal_kind=ProposalKind.MERGE_CONCEPTS,
                action_kind=ActionKind.MERGE_CONCEPTS,
                action_path="concepts/foo",
                payload={"a": 1},
                status=ProposalStatus.PENDING,
                score=80,
                created_at=datetime.now(tz=UTC),
            )
        )
        s.add(
            CanonicalizationProposal(
                id=uuid.uuid4(),
                workspace_id=workspace_id,
                proposal_kind=ProposalKind.CREATE_CONCEPT,
                action_kind=ActionKind.CREATE_CONCEPT,
                action_path="concepts/bar",
                payload={"b": 2},
                status=ProposalStatus.APPROVED,
                score=50,
                created_at=datetime.now(tz=UTC),
            )
        )
        await s.commit()

    r = await client.get("/api/v1/decisions")
    assert r.status_code == 200
    assert len(r.json()) == 2

    # Filter to pending
    r = await client.get("/api/v1/decisions", params={"status_filter": "pending"})
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["status"] == "pending"


async def test_decisions_log(client, db, workspace_id) -> None:
    async with db() as s:
        s.add(
            CanonicalizationDecision(
                id=uuid.uuid4(),
                workspace_id=workspace_id,
                decision_kind=DecisionKind.CANNOT_LINK,
                actor_id=uuid.uuid4(),
                rationale="not the same concept",
                created_at=datetime.now(tz=UTC),
            )
        )
        await s.commit()
    r = await client.get("/api/v1/decisions/log")
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["decision_kind"] == "cannot-link"


async def test_decisions_workspace_isolation(client, db, workspace_id) -> None:
    other = uuid.uuid4()
    async with db() as s:
        s.add(
            CanonicalizationProposal(
                id=uuid.uuid4(),
                workspace_id=other,
                proposal_kind=ProposalKind.CREATE_CONCEPT,
                action_kind=ActionKind.CREATE_CONCEPT,
                action_path="concepts/other",
                payload={},
                status=ProposalStatus.PENDING,
                created_at=datetime.now(tz=UTC),
            )
        )
        await s.commit()
    r = await client.get("/api/v1/decisions")
    assert r.status_code == 200
    assert r.json() == []
