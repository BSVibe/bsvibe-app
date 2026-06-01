"""GDPR L1 endpoints — /api/v1/workspace/export + /processing-record.

Covers Art. 15 (right to access), Art. 20 (portability) and Art. 30
(processing record).
"""

from __future__ import annotations

import uuid

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.api.deps import (
    get_current_user,
    get_db_session,
    get_workspace_id,
)
from backend.api.main import create_app
from backend.identity.db import MembershipRow, UserRow
from backend.identity.workspaces_db import WorkspaceRow, WorkspacesBase

from .._support import db_engine, fake_current_user

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def db():
    async with db_engine(WorkspacesBase) as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


@pytest_asyncio.fixture
async def client_with_ws(db):
    """Client + a workspace + a user with an active membership."""
    app = create_app()
    workspace_id = uuid.uuid4()
    user_id = uuid.uuid4()

    def _ws() -> uuid.UUID:
        return workspace_id

    async def _session():
        async with db() as s:
            yield s

    app.dependency_overrides[get_current_user] = fake_current_user()
    app.dependency_overrides[get_workspace_id] = _ws
    app.dependency_overrides[get_db_session] = _session

    async with db() as s:
        s.add(
            WorkspaceRow(
                id=workspace_id,
                name="Acme",
                region="eu-1",
                safe_mode=True,
                legal_basis="contract",
            )
        )
        s.add(UserRow(id=user_id, supabase_user_id="test-user", email="t@example.com"))
        await s.flush()
        s.add(
            MembershipRow(id=uuid.uuid4(), user_id=user_id, workspace_id=workspace_id, role="owner")
        )
        await s.commit()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c, workspace_id, user_id


async def test_export_returns_profile_and_workspace(client_with_ws) -> None:
    c, workspace_id, user_id = client_with_ws
    r = await c.get("/api/v1/workspace/export")
    assert r.status_code == 200, r.text
    body = r.json()
    # Expected top-level shape — stable contract for portability.
    for key in (
        "workspace",
        "profile",
        "products",
        "product_resources",
        "resource_bindings",
        "runs",
        "deliverables",
        "decisions",
        "knowledge_concepts",
        "exported_at",
    ):
        assert key in body, f"missing key {key} in {sorted(body.keys())}"
    assert body["workspace"]["id"] == str(workspace_id)
    assert body["workspace"]["legal_basis"] == "contract"
    assert body["profile"]["user_id"] == str(user_id)
    assert body["profile"]["email"] == "t@example.com"
    assert body["profile"]["membership"]["role"] == "owner"


async def test_export_unauthenticated_rejected(db) -> None:
    """No fake user → 401 from the v1 router-level get_current_user dep."""
    app = create_app()

    async def _session():
        async with db() as s:
            yield s

    app.dependency_overrides[get_db_session] = _session
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get("/api/v1/workspace/export")
        assert r.status_code == 401


async def test_processing_record_returns_art30_doc(client_with_ws) -> None:
    c, workspace_id, _ = client_with_ws
    r = await c.get("/api/v1/workspace/processing-record")
    assert r.status_code == 200, r.text
    body = r.json()
    # Art. 30 minimum required fields.
    for key in (
        "controller",
        "purposes",
        "categories_of_data",
        "categories_of_recipients",
        "sub_processors",
        "retention",
        "security_measures",
        "legal_basis",
        "workspace_id",
        "region",
        "generated_at",
    ):
        assert key in body, f"missing Art. 30 key {key} in {sorted(body.keys())}"
    assert body["workspace_id"] == str(workspace_id)
    assert body["region"] == "eu-1"
    assert body["legal_basis"] == "contract"
    assert isinstance(body["sub_processors"], list)
    # The sub-processor list must include the obvious ones.
    names = [sp["name"].lower() for sp in body["sub_processors"]]
    assert any("supabase" in n for n in names)
