"""``/api/v1/workspace`` — GET + PATCH the caller's workspace name.

Tests the everyday workspace metadata routes registered alongside the
compliance ones under the singular ``/workspace`` prefix.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select
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
                region="us-1",
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
        yield c, workspace_id, db


async def test_get_workspace_returns_id_and_name(client_with_ws) -> None:
    c, workspace_id, _ = client_with_ws
    r = await c.get("/api/v1/workspace")
    assert r.status_code == 200, r.text
    body = r.json()
    # Lift Q1 — ``audit_retention_days`` surfaces on the GET response; default ``None``
    # (= forever) until a PATCH sets it.
    assert body == {
        "id": str(workspace_id),
        "name": "Acme",
        "audit_retention_days": None,
        # Lift E1 — workspace-default ModelAccount fallback surfaces here;
        # ``None`` until the founder picks one via PATCH or MCP.
        "default_account_id": None,
        "language": "en",
        # N1b — the IANA time zone the server-side quiet-hours gate reads;
        # default ``"UTC"`` (the multi-tenant global default).
        "timezone": "UTC",
        # L3 (#5) — Safe Mode surfaces here; seeded ``True`` (the default).
        "safe_mode": True,
    }


async def test_patch_workspace_renames_and_persists(client_with_ws) -> None:
    c, workspace_id, db = client_with_ws
    r = await c.patch("/api/v1/workspace", json={"name": "Acme Inc."})
    assert r.status_code == 200, r.text
    assert r.json() == {
        "id": str(workspace_id),
        "name": "Acme Inc.",
        "audit_retention_days": None,
        "default_account_id": None,
        "language": "en",
        "timezone": "UTC",
        "safe_mode": True,
    }

    # The row in the database actually changed.
    async with db() as s:
        row = (
            await s.execute(select(WorkspaceRow).where(WorkspaceRow.id == workspace_id))
        ).scalar_one()
        assert row.name == "Acme Inc."
        assert row.audit_retention_days is None


async def test_patch_workspace_sets_audit_retention_days(client_with_ws) -> None:
    """Lift Q1 — a workspace opts INTO N-day rotation by PATCHing the field."""
    c, workspace_id, db = client_with_ws
    r = await c.patch("/api/v1/workspace", json={"audit_retention_days": 30})
    assert r.status_code == 200, r.text
    assert r.json() == {
        "id": str(workspace_id),
        "name": "Acme",  # unchanged
        "audit_retention_days": 30,
        "default_account_id": None,
        "language": "en",
        "timezone": "UTC",
        "safe_mode": True,
    }
    async with db() as s:
        row = (
            await s.execute(select(WorkspaceRow).where(WorkspaceRow.id == workspace_id))
        ).scalar_one()
        assert row.audit_retention_days == 30
        assert row.name == "Acme"  # untouched by retention-only PATCH


async def test_patch_workspace_sets_language(client_with_ws) -> None:
    """#6 — the founder sets the workspace's LLM output language; it persists and
    is threaded into generation prompts. Default is 'en'."""
    c, workspace_id, db = client_with_ws
    r = await c.patch("/api/v1/workspace", json={"language": "ko"})
    assert r.status_code == 200, r.text
    assert r.json()["language"] == "ko"
    async with db() as s:
        row = (
            await s.execute(select(WorkspaceRow).where(WorkspaceRow.id == workspace_id))
        ).scalar_one()
        assert row.language == "ko"
    # An unsupported tag is rejected (422), never silently stored.
    bad = await c.patch("/api/v1/workspace", json={"language": "fr"})
    assert bad.status_code == 422


async def test_patch_workspace_sets_timezone(client_with_ws) -> None:
    """N1b — the founder sets the workspace's IANA time zone; it persists so the
    server-side NotifyWorker can evaluate quiet hours. Default is 'UTC'; an
    invalid IANA zone is rejected (422), never silently stored."""
    c, workspace_id, db = client_with_ws
    r = await c.patch("/api/v1/workspace", json={"timezone": "Asia/Seoul"})
    assert r.status_code == 200, r.text
    assert r.json()["timezone"] == "Asia/Seoul"
    async with db() as s:
        row = (
            await s.execute(select(WorkspaceRow).where(WorkspaceRow.id == workspace_id))
        ).scalar_one()
        assert row.timezone == "Asia/Seoul"
    # A name-only PATCH must not disturb the time zone.
    r = await c.patch("/api/v1/workspace", json={"name": "Renamed"})
    assert r.status_code == 200, r.text
    assert r.json()["timezone"] == "Asia/Seoul"
    # A non-existent IANA zone is rejected, never silently stored.
    bad = await c.patch("/api/v1/workspace", json={"timezone": "Mars/Phobos"})
    assert bad.status_code == 422
    async with db() as s:
        row = (
            await s.execute(select(WorkspaceRow).where(WorkspaceRow.id == workspace_id))
        ).scalar_one()
        assert row.timezone == "Asia/Seoul"  # unchanged by the rejected PATCH


async def test_patch_workspace_sets_safe_mode(client_with_ws) -> None:
    """L3 (#5) — the founder switches to Auto mode by PATCHing safe_mode=false;
    it persists and surfaces on the response. A name-only PATCH leaves it alone."""
    c, _workspace_id, db = client_with_ws
    # Switch to Auto (deliverables auto-dispatch instead of queueing).
    r = await c.patch("/api/v1/workspace", json={"safe_mode": False})
    assert r.status_code == 200, r.text
    assert r.json()["safe_mode"] is False
    async with db() as s:
        row = (await s.execute(select(WorkspaceRow))).scalar_one()
        assert row.safe_mode is False
    # A name-only PATCH must not flip the mode back.
    r = await c.patch("/api/v1/workspace", json={"name": "Renamed"})
    assert r.status_code == 200, r.text
    assert r.json()["safe_mode"] is False
    # Switch back to Safe.
    r = await c.patch("/api/v1/workspace", json={"safe_mode": True})
    assert r.json()["safe_mode"] is True


async def test_patch_workspace_unsets_audit_retention_days_to_forever(client_with_ws) -> None:
    """Lift Q1 — explicit ``null`` clears the retention back to forever (the default)."""
    c, _workspace_id, db = client_with_ws
    # First opt-in.
    r = await c.patch("/api/v1/workspace", json={"audit_retention_days": 14})
    assert r.status_code == 200
    # Then explicitly unset.
    r = await c.patch("/api/v1/workspace", json={"audit_retention_days": None})
    assert r.status_code == 200, r.text
    assert r.json()["audit_retention_days"] is None
    async with db() as s:
        row = (await s.execute(select(WorkspaceRow))).scalar_one()
        assert row.audit_retention_days is None


async def test_patch_workspace_rejects_zero_or_negative_retention(client_with_ws) -> None:
    """Lift Q1 — ``ge=1`` enforces "N >= 1 days" half of the column's contract."""
    c, _workspace_id, _ = client_with_ws
    r = await c.patch("/api/v1/workspace", json={"audit_retention_days": 0})
    assert r.status_code == 422
    r = await c.patch("/api/v1/workspace", json={"audit_retention_days": -7})
    assert r.status_code == 422


async def test_patch_workspace_trims_whitespace(client_with_ws) -> None:
    c, workspace_id, _ = client_with_ws
    r = await c.patch("/api/v1/workspace", json={"name": "   Renamed Co.   "})
    assert r.status_code == 200
    assert r.json()["name"] == "Renamed Co."


async def test_patch_workspace_rejects_empty_name(client_with_ws) -> None:
    c, _workspace_id, _ = client_with_ws
    r = await c.patch("/api/v1/workspace", json={"name": ""})
    assert r.status_code == 422  # Field(min_length=1)


async def test_patch_workspace_rejects_extra_fields(client_with_ws) -> None:
    """``extra="forbid"`` — unknown keys (e.g. region, legal_basis) are
    rejected so writes can't quietly mutate fields the route doesn't own."""
    c, _workspace_id, _ = client_with_ws
    r = await c.patch("/api/v1/workspace", json={"name": "Renamed", "region": "moon-1"})
    assert r.status_code == 422


async def test_get_workspace_unauthenticated_rejected(db) -> None:
    app = create_app()

    async def _session():
        async with db() as s:
            yield s

    app.dependency_overrides[get_db_session] = _session
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get("/api/v1/workspace")
        assert r.status_code == 401
