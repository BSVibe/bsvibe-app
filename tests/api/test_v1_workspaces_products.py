"""/api/v1/workspaces + /api/v1/products — full CRUD against real PG."""

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
from backend.identity.db import MembershipRow, UserRow  # noqa: F401 — register tables
from backend.identity.workspaces_db import WorkspacesBase
from backend.workflow.infrastructure.db import (  # noqa: F401 — register run tables
    ExecutionBase,
    ExecutionRun,
    RunStatus,
)

from .._support import db_engine, fake_current_user

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def db():
    async with db_engine(WorkspacesBase, ExecutionBase) as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


@pytest_asyncio.fixture
async def client_with_ws(db):
    """Client + a pre-created workspace + dep override."""
    app = create_app()
    workspace_id = uuid.uuid4()

    def _ws() -> uuid.UUID:
        return workspace_id

    async def _session():
        async with db() as s:
            yield s

    app.dependency_overrides[get_current_user] = fake_current_user()
    app.dependency_overrides[get_workspace_id] = _ws
    app.dependency_overrides[get_db_session] = _session

    # Seed the workspace row so /api/v1/products has a parent, plus an owner
    # membership for the fake principal so role-gated routes (product DELETE
    # requires admin+) resolve a real Membership.role.
    from backend.identity.workspaces_db import WorkspaceRow

    async with db() as s:
        s.add(WorkspaceRow(id=workspace_id, name="test", region="us-1", safe_mode=True))
        user = UserRow(id=uuid.uuid4(), supabase_user_id="test-user", email="t@example.com")
        s.add(user)
        await s.flush()
        s.add(
            MembershipRow(id=uuid.uuid4(), user_id=user.id, workspace_id=workspace_id, role="owner")
        )
        await s.commit()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c, workspace_id


async def test_workspaces_full_lifecycle(db) -> None:
    app = create_app()

    async def _session():
        async with db() as s:
            yield s

    # Seed a real user row so the FK from memberships → users holds on PG
    # (SQLite doesn't enforce it, but real Postgres does).
    async with db() as s:
        s.add(UserRow(id=uuid.uuid4(), supabase_user_id="test-user", email="t@x"))
        await s.commit()

    app.dependency_overrides[get_db_session] = _session
    app.dependency_overrides[get_current_user] = fake_current_user("test-user")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        # Initially empty
        r = await c.get("/api/v1/workspaces")
        assert r.status_code == 200
        assert r.json() == []

        # Create
        r = await c.post(
            "/api/v1/workspaces",
            json={"name": "Acme", "region": "us-1", "safe_mode": False},
        )
        assert r.status_code == 201, r.text
        created = r.json()
        ws_id = created["id"]
        assert created["safe_mode"] is False

        # List
        r = await c.get("/api/v1/workspaces")
        assert len(r.json()) == 1

        # Get
        r = await c.get(f"/api/v1/workspaces/{ws_id}")
        assert r.status_code == 200

        # Patch
        r = await c.patch(f"/api/v1/workspaces/{ws_id}", json={"region": "eu-1"})
        assert r.status_code == 200
        assert r.json()["region"] == "eu-1"

        # Delete
        r = await c.delete(f"/api/v1/workspaces/{ws_id}")
        assert r.status_code == 204
        r = await c.get(f"/api/v1/workspaces/{ws_id}")
        assert r.status_code == 404


async def test_products_full_lifecycle(client_with_ws) -> None:
    c, workspace_id = client_with_ws
    # Initial empty
    r = await c.get("/api/v1/products")
    assert r.status_code == 200
    assert r.json() == []

    # Create
    r = await c.post(
        "/api/v1/products",
        json={"name": "My Blog", "slug": "my-blog", "repo_url": "https://x/y"},
    )
    assert r.status_code == 201, r.text
    product_id = r.json()["id"]

    # Slug conflict
    r = await c.post("/api/v1/products", json={"name": "Other", "slug": "my-blog"})
    assert r.status_code == 409

    # Invalid slug format
    r = await c.post("/api/v1/products", json={"name": "X", "slug": "Bad Slug"})
    assert r.status_code == 422

    # List
    r = await c.get("/api/v1/products")
    assert len(r.json()) == 1

    # Patch
    r = await c.patch(f"/api/v1/products/{product_id}", json={"name": "Renamed"})
    assert r.status_code == 200
    assert r.json()["name"] == "Renamed"

    # Delete
    r = await c.delete(f"/api/v1/products/{product_id}")
    assert r.status_code == 204


async def test_delete_product_cascade_cancels_runs(client_with_ws, db) -> None:
    """Deleting a product cancels its non-terminal runs (no orphans); terminal
    runs and other products' runs are untouched."""
    c, workspace_id = client_with_ws
    r = await c.post("/api/v1/products", json={"name": "P", "slug": "p"})
    assert r.status_code == 201, r.text
    product_id = uuid.UUID(r.json()["id"])

    import datetime as _dt

    async with db() as s:
        open_run = ExecutionRun(
            id=uuid.uuid4(),
            workspace_id=workspace_id,
            product_id=product_id,
            status=RunStatus.REVIEW_READY,
            payload={},
            created_at=_dt.datetime.now(_dt.UTC),
            updated_at=_dt.datetime.now(_dt.UTC),
        )
        shipped_run = ExecutionRun(
            id=uuid.uuid4(),
            workspace_id=workspace_id,
            product_id=product_id,
            status=RunStatus.SHIPPED,
            payload={},
            created_at=_dt.datetime.now(_dt.UTC),
            updated_at=_dt.datetime.now(_dt.UTC),
        )
        s.add_all([open_run, shipped_run])
        await s.commit()
        open_id, shipped_id = open_run.id, shipped_run.id

    r = await c.delete(f"/api/v1/products/{product_id}")
    assert r.status_code == 204, r.text

    async with db() as s:
        assert (await s.get(ExecutionRun, open_id)).status is RunStatus.CANCELLED
        assert (await s.get(ExecutionRun, shipped_id)).status is RunStatus.SHIPPED


async def test_create_product_initialises_git_workspace(client_with_ws) -> None:
    """W1: every ProductRow create call provisions a real git repo at
    ``var/products/<product_id>/`` so the next AgentRunner can branch a
    worktree off ``main`` (no lazy init needed at first-run time, though
    the provisioner does it as a safety net for legacy rows).
    """
    import uuid as _uuid  # noqa: PLC0415 — local-only

    from backend.storage.product_workspace import product_workspace_path  # noqa: PLC0415

    c, _ = client_with_ws
    r = await c.post(
        "/api/v1/products",
        json={"name": "Workspace Probe", "slug": "ws-probe"},
    )
    assert r.status_code == 201, r.text
    product_id = _uuid.UUID(r.json()["id"])

    path = product_workspace_path(product_id)
    assert path.is_dir(), "product workspace dir must exist after create"
    assert (path / ".git").is_dir(), "must be a real git repo"
    assert (path / ".bsvibe" / "PRODUCT.md").is_file(), (
        "initial commit's marker must be checked out"
    )


async def test_create_with_repo_url_schedules_bootstrap(client_with_ws) -> None:
    """Lift A v2 — create with ``repo_url`` stamps pending + schedules job."""
    from backend.api.v1.products.products_crud import get_bootstrap_scheduler  # noqa: PLC0415

    c, _ = client_with_ws
    captured: dict[str, object] = {}

    def _fake_scheduler(**kwargs):
        captured.update(kwargs)
        return None

    c._transport.app.dependency_overrides[get_bootstrap_scheduler] = (  # type: ignore[attr-defined]
        lambda: _fake_scheduler
    )

    r = await c.post(
        "/api/v1/products",
        json={
            "name": "Repo bound",
            "slug": "repo-bound",
            "repo_url": "https://github.com/org/repo",
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["bootstrap_status"] == "pending"
    assert body["bootstrap_artifacts_count"] is None
    assert body["bootstrap_error"] is None

    # The fake scheduler captured the kwargs the runtime will receive.
    assert "product_id" in captured
    assert captured["repo_url"] == "https://github.com/org/repo"


async def test_create_without_repo_url_skips_bootstrap(client_with_ws) -> None:
    """Lift A v2 — no repo_url → no bootstrap_status, scheduler not called."""
    from backend.api.v1.products.products_crud import get_bootstrap_scheduler  # noqa: PLC0415

    c, _ = client_with_ws
    invoked = []

    def _fake_scheduler(**kwargs):
        invoked.append(kwargs)
        return None

    c._transport.app.dependency_overrides[get_bootstrap_scheduler] = (  # type: ignore[attr-defined]
        lambda: _fake_scheduler
    )

    r = await c.post("/api/v1/products", json={"name": "Plain", "slug": "plain"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["bootstrap_status"] is None
    assert invoked == []


async def test_get_bootstrap_returns_progress(client_with_ws) -> None:
    """Lift A v2 — ``GET /products/{id}/bootstrap`` reads the row's status."""
    from backend.api.v1.products.products_crud import get_bootstrap_scheduler  # noqa: PLC0415

    c, _ = client_with_ws
    c._transport.app.dependency_overrides[get_bootstrap_scheduler] = (  # type: ignore[attr-defined]
        lambda: lambda **_: None
    )

    r = await c.post(
        "/api/v1/products",
        json={
            "name": "With repo",
            "slug": "with-repo",
            "repo_url": "https://github.com/x/y",
        },
    )
    assert r.status_code == 201
    pid = r.json()["id"]

    r = await c.get(f"/api/v1/products/{pid}/bootstrap")
    assert r.status_code == 200, r.text
    progress = r.json()
    assert progress["product_id"] == pid
    assert progress["status"] == "pending"
    assert progress["artifacts_count"] is None
    assert progress["error"] is None


async def test_get_bootstrap_404_for_unknown_product(client_with_ws) -> None:
    """Lift A v2 — bootstrap endpoint is workspace-scoped (404 on miss)."""
    import uuid as _uuid  # noqa: PLC0415 — local-only

    c, _ = client_with_ws
    r = await c.get(f"/api/v1/products/{_uuid.uuid4()}/bootstrap")
    assert r.status_code == 404


async def test_product_workspace_isolation(db) -> None:
    """A product in workspace A is NOT visible / patchable from workspace B."""
    app = create_app()
    ws_a = uuid.uuid4()
    ws_b = uuid.uuid4()

    async def _session():
        async with db() as s:
            yield s

    app.dependency_overrides[get_db_session] = _session
    app.dependency_overrides[get_current_user] = fake_current_user()
    from backend.identity.workspaces_db import ProductRow, WorkspaceRow

    product_id = uuid.uuid4()
    async with db() as s:
        s.add(WorkspaceRow(id=ws_a, name="a", region="us-1", safe_mode=True))
        s.add(WorkspaceRow(id=ws_b, name="b", region="us-1", safe_mode=True))
        await s.flush()
        s.add(ProductRow(id=product_id, workspace_id=ws_a, name="A's blog", slug="a-blog"))
        await s.commit()

    transport = httpx.ASGITransport(app=app)
    # Workspace B's view
    app.dependency_overrides[get_workspace_id] = lambda: ws_b
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get("/api/v1/products")
        assert r.json() == []
        r = await c.get(f"/api/v1/products/{product_id}")
        assert r.status_code == 404
