"""/api/v1/products/{product_id}/bindings — per-Product × Connector 3-knob CRUD.

A Resource binding (Workflow §3) carries **selection**, **trigger
{enabled, filters}**, and **output_mode {safe|direct}** for one Product against
one ConnectorAccount + a connector-side ``resource_id``.

Workspace-scoped exactly like the parent product: a binding for a product in
workspace A is invisible / 404 from workspace B; adding to a product the caller
can't see 404s.
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
from backend.connectors.db import ConnectorAccountRow
from backend.identity.db import MembershipRow, UserRow  # noqa: F401 — register tables
from backend.workspaces.db import ProductRow, WorkspaceRow, WorkspacesBase

from .._support import db_engine, fake_current_user

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def db():
    async with db_engine(WorkspacesBase) as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


@pytest_asyncio.fixture
async def client_with_parents(db):
    """Client + a seeded workspace + a product + a ConnectorAccount.

    FK-safe seeding: parents are flushed before any binding is inserted.
    """
    app = create_app()
    workspace_id = uuid.uuid4()
    product_id = uuid.uuid4()
    connector_account_id = uuid.uuid4()

    async def _session():
        async with db() as s:
            yield s

    app.dependency_overrides[get_current_user] = fake_current_user()
    app.dependency_overrides[get_workspace_id] = lambda: workspace_id
    app.dependency_overrides[get_db_session] = _session

    async with db() as s:
        s.add(WorkspaceRow(id=workspace_id, name="test", region="us-1", safe_mode=True))
        await s.flush()
        s.add(ProductRow(id=product_id, workspace_id=workspace_id, name="Blog", slug="blog"))
        s.add(
            ConnectorAccountRow(
                id=connector_account_id,
                workspace_id=workspace_id,
                connector="github",
                webhook_token=f"tok-{uuid.uuid4().hex}",
                signing_secret_ciphertext="cipher",
            )
        )
        await s.commit()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c, workspace_id, product_id, connector_account_id


async def test_create_binding_persists_and_lists(client_with_parents) -> None:
    c, _ws, product_id, conn_id = client_with_parents

    # Initially empty.
    r = await c.get(f"/api/v1/products/{product_id}/bindings")
    assert r.status_code == 200
    assert r.json() == []

    # Create with full knobs.
    r = await c.post(
        f"/api/v1/products/{product_id}/bindings",
        json={
            "connector_account_id": str(conn_id),
            "resource_id": "bsvibe/bsvibe-site",
            "selection": {"labels": ["bug"]},
            "trigger": {"enabled": True, "filters": {"branch": "main"}},
            "output_mode": "direct",
        },
    )
    assert r.status_code == 201, r.text
    created = r.json()
    assert created["product_id"] == str(product_id)
    assert created["connector_account_id"] == str(conn_id)
    assert created["resource_id"] == "bsvibe/bsvibe-site"
    assert created["selection"] == {"labels": ["bug"]}
    assert created["trigger"] == {"enabled": True, "filters": {"branch": "main"}}
    assert created["output_mode"] == "direct"

    # Now listed.
    r = await c.get(f"/api/v1/products/{product_id}/bindings")
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["id"] == created["id"]


async def test_create_binding_defaults_safe_and_disabled(client_with_parents) -> None:
    c, _ws, product_id, conn_id = client_with_parents
    r = await c.post(
        f"/api/v1/products/{product_id}/bindings",
        json={"connector_account_id": str(conn_id), "resource_id": "r"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    # Spec defaults.
    assert body["output_mode"] == "safe"
    assert body["trigger"] == {"enabled": False, "filters": {}}
    assert body["selection"] == {}


async def test_create_binding_rejects_invalid_output_mode(client_with_parents) -> None:
    c, _ws, product_id, conn_id = client_with_parents
    r = await c.post(
        f"/api/v1/products/{product_id}/bindings",
        json={
            "connector_account_id": str(conn_id),
            "resource_id": "r",
            "output_mode": "loud",
        },
    )
    assert r.status_code == 422


async def test_create_binding_rejects_extra_field(client_with_parents) -> None:
    c, _ws, product_id, conn_id = client_with_parents
    r = await c.post(
        f"/api/v1/products/{product_id}/bindings",
        json={
            "connector_account_id": str(conn_id),
            "resource_id": "r",
            "bogus": "nope",
        },
    )
    assert r.status_code == 422


async def test_create_binding_rejects_unknown_connector_account(client_with_parents) -> None:
    c, _ws, product_id, _conn_id = client_with_parents
    missing = uuid.uuid4()
    r = await c.post(
        f"/api/v1/products/{product_id}/bindings",
        json={"connector_account_id": str(missing), "resource_id": "r"},
    )
    assert r.status_code == 404


async def test_create_binding_unknown_product_404(client_with_parents) -> None:
    c, _ws, _product_id, conn_id = client_with_parents
    missing = uuid.uuid4()
    r = await c.post(
        f"/api/v1/products/{missing}/bindings",
        json={"connector_account_id": str(conn_id), "resource_id": "r"},
    )
    assert r.status_code == 404
    r = await c.get(f"/api/v1/products/{missing}/bindings")
    assert r.status_code == 404


async def test_update_binding_changes_knobs(client_with_parents) -> None:
    c, _ws, product_id, conn_id = client_with_parents
    r = await c.post(
        f"/api/v1/products/{product_id}/bindings",
        json={"connector_account_id": str(conn_id), "resource_id": "r"},
    )
    binding_id = r.json()["id"]

    r = await c.patch(
        f"/api/v1/products/{product_id}/bindings/{binding_id}",
        json={
            "output_mode": "direct",
            "trigger": {"enabled": True, "filters": {"k": "v"}},
            "selection": {"folder": "inbox"},
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["output_mode"] == "direct"
    assert body["trigger"] == {"enabled": True, "filters": {"k": "v"}}
    assert body["selection"] == {"folder": "inbox"}


async def test_update_binding_rejects_invalid_output_mode(client_with_parents) -> None:
    c, _ws, product_id, conn_id = client_with_parents
    r = await c.post(
        f"/api/v1/products/{product_id}/bindings",
        json={"connector_account_id": str(conn_id), "resource_id": "r"},
    )
    binding_id = r.json()["id"]

    r = await c.patch(
        f"/api/v1/products/{product_id}/bindings/{binding_id}",
        json={"output_mode": "shouted"},
    )
    assert r.status_code == 422


async def test_delete_binding(client_with_parents) -> None:
    c, _ws, product_id, conn_id = client_with_parents
    r = await c.post(
        f"/api/v1/products/{product_id}/bindings",
        json={"connector_account_id": str(conn_id), "resource_id": "r"},
    )
    binding_id = r.json()["id"]

    r = await c.delete(f"/api/v1/products/{product_id}/bindings/{binding_id}")
    assert r.status_code == 204

    r = await c.get(f"/api/v1/products/{product_id}/bindings")
    assert r.json() == []

    # Deleting again 404s.
    r = await c.delete(f"/api/v1/products/{product_id}/bindings/{binding_id}")
    assert r.status_code == 404


async def test_binding_workspace_isolation(db) -> None:
    """A binding in workspace A is invisible from workspace B."""
    app = create_app()
    ws_a = uuid.uuid4()
    ws_b = uuid.uuid4()
    product_a = uuid.uuid4()
    conn_a = uuid.uuid4()

    async def _session():
        async with db() as s:
            yield s

    app.dependency_overrides[get_db_session] = _session
    app.dependency_overrides[get_current_user] = fake_current_user()

    async with db() as s:
        s.add(WorkspaceRow(id=ws_a, name="a", region="us-1", safe_mode=True))
        s.add(WorkspaceRow(id=ws_b, name="b", region="us-1", safe_mode=True))
        await s.flush()
        s.add(ProductRow(id=product_a, workspace_id=ws_a, name="A blog", slug="a-blog"))
        s.add(
            ConnectorAccountRow(
                id=conn_a,
                workspace_id=ws_a,
                connector="github",
                webhook_token=f"tok-{uuid.uuid4().hex}",
                signing_secret_ciphertext="cipher",
            )
        )
        await s.commit()

    transport = httpx.ASGITransport(app=app)

    # Create one binding as workspace A.
    app.dependency_overrides[get_workspace_id] = lambda: ws_a
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post(
            f"/api/v1/products/{product_a}/bindings",
            json={"connector_account_id": str(conn_a), "resource_id": "r"},
        )
        assert r.status_code == 201, r.text
        binding_id = r.json()["id"]

    # Workspace B can't see workspace A's product → 404 on bindings list / delete.
    app.dependency_overrides[get_workspace_id] = lambda: ws_b
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get(f"/api/v1/products/{product_a}/bindings")
        assert r.status_code == 404
        r = await c.delete(f"/api/v1/products/{product_a}/bindings/{binding_id}")
        assert r.status_code == 404

    # Workspace A still sees its binding.
    app.dependency_overrides[get_workspace_id] = lambda: ws_a
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get(f"/api/v1/products/{product_a}/bindings")
        assert r.status_code == 200
        assert len(r.json()) == 1
