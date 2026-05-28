"""/api/v1/products/{id}/files — lazy product-repo file-tree browser + content.

Drives the real subprocess-git product workspace under a tmp
``product_workspace_root`` (the POST create inits the repo; we commit a few
files onto main) and asserts the listing is one-level + the content read serves
the shipped file and 404s safely off-path."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.api.deps import get_current_user, get_db_session, get_workspace_id
from backend.api.main import create_app
from backend.config import get_settings
from backend.identity.db import MembershipRow, UserRow  # noqa: F401 — register tables
from backend.storage.product_workspace import product_workspace_path

from .._support import db_engine, fake_current_user

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def product_root(tmp_path: Path, monkeypatch):
    """Point product_workspace_root at a tmp dir + clear the settings cache so
    the request-time git init/read lands in the test sandbox."""
    root = tmp_path / "products"
    root.mkdir()
    monkeypatch.setenv("BSVIBE_PRODUCT_WORKSPACE_ROOT", str(root))
    get_settings.cache_clear()
    yield root
    get_settings.cache_clear()


@pytest_asyncio.fixture
async def db():
    from backend.workspaces.db import WorkspacesBase

    async with db_engine(WorkspacesBase) as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


@pytest_asyncio.fixture
async def client_ws(db):
    from backend.workspaces.db import WorkspaceRow

    app = create_app()
    workspace_id = uuid.uuid4()

    async def _session():
        async with db() as s:
            yield s

    app.dependency_overrides[get_current_user] = fake_current_user()
    app.dependency_overrides[get_workspace_id] = lambda: workspace_id
    app.dependency_overrides[get_db_session] = _session

    async with db() as s:
        s.add(WorkspaceRow(id=workspace_id, name="t", region="us-1", safe_mode=True))
        user = UserRow(id=uuid.uuid4(), supabase_user_id="u", email="u@x")
        s.add(user)
        await s.flush()
        s.add(
            MembershipRow(id=uuid.uuid4(), user_id=user.id, workspace_id=workspace_id, role="owner")
        )
        await s.commit()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c, workspace_id


async def _git(*args: str, cwd: Path) -> None:
    proc = await asyncio.create_subprocess_exec(
        "git", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, cwd=str(cwd)
    )
    _out, err = await proc.communicate()
    assert proc.returncode == 0, err.decode()


async def _create_product(c: httpx.AsyncClient) -> str:
    r = await c.post("/api/v1/products", json={"name": "P", "slug": "p"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _commit_files(product_id: str, files: dict[str, str]) -> None:
    repo = product_workspace_path(uuid.UUID(product_id))
    for rel, content in files.items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    await _git("add", "-A", cwd=repo)
    await _git("commit", "-m", "seed", cwd=repo)


async def test_files_list_one_level_dirs_before_files(client_ws) -> None:
    c, _ws = client_ws
    pid = await _create_product(c)
    await _commit_files(pid, {"README.md": "# hi\n", "src/app.py": "x = 1\n"})

    r = await c.get(f"/api/v1/products/{pid}/files")
    assert r.status_code == 200, r.text
    rows = r.json()
    # .bsvibe (init) + src are dirs (sorted first), README.md a file.
    assert [(e["name"], e["kind"]) for e in rows] == [
        (".bsvibe", "dir"),
        ("src", "dir"),
        ("README.md", "file"),
    ]

    # Lazy: a subdir lists only its immediate children with full paths.
    r = await c.get(f"/api/v1/products/{pid}/files", params={"path": "src"})
    assert r.status_code == 200, r.text
    assert [(e["name"], e["path"], e["kind"]) for e in r.json()] == [
        ("app.py", "src/app.py", "file"),
    ]


async def test_files_content_serves_committed_file(client_ws) -> None:
    c, _ws = client_ws
    pid = await _create_product(c)
    await _commit_files(pid, {"src/app.py": "print('hi')\n"})

    r = await c.get(f"/api/v1/products/{pid}/files/content", params={"path": "src/app.py"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["path"] == "src/app.py"
    assert body["content"] == "print('hi')\n"
    assert body["binary"] is False


async def test_files_content_missing_and_traversal_404(client_ws) -> None:
    c, _ws = client_ws
    pid = await _create_product(c)

    r = await c.get(f"/api/v1/products/{pid}/files/content", params={"path": "nope.py"})
    assert r.status_code == 404
    r = await c.get(f"/api/v1/products/{pid}/files/content", params={"path": "../../etc/passwd"})
    assert r.status_code == 404


async def test_files_cross_workspace_404(client_ws) -> None:
    c, _ws = client_ws
    # A product id that isn't in this workspace → 404 (never reaches git).
    r = await c.get(f"/api/v1/products/{uuid.uuid4()}/files")
    assert r.status_code == 404
