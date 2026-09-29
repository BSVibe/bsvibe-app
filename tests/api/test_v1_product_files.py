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

from .._support import db_engine, fake_current_user, publishing_workspace
from .conftest import commit_per_workspace, flush_per_workspace

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
    from backend.identity.workspaces_db import WorkspacesBase

    async with db_engine(WorkspacesBase) as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


@pytest_asyncio.fixture
async def client_ws(db):
    from backend.identity.workspaces_db import WorkspaceRow

    app = create_app()
    workspace_id = uuid.uuid4()

    async def _session():
        async with db() as s:
            yield s

    app.dependency_overrides[get_current_user] = fake_current_user()
    app.dependency_overrides[get_workspace_id] = publishing_workspace(workspace_id)
    app.dependency_overrides[get_db_session] = _session

    async with db() as s:
        s.add(WorkspaceRow(id=workspace_id, name="t", safe_mode=True))
        user = UserRow(id=uuid.uuid4(), supabase_user_id="u", email="u@x")
        s.add(user)
        await flush_per_workspace(s)
        s.add(
            MembershipRow(id=uuid.uuid4(), user_id=user.id, workspace_id=workspace_id, role="owner")
        )
        await commit_per_workspace(s)

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


# ── #1042 — 검색 엔드포인트 ────────────────────────────────────────────────


async def test_files_search_finds_a_file_in_an_unexpanded_directory(client_ws) -> None:
    """브라우저가 못 하는 일이 이것이다 — 트리는 한 단계씩 게으르게 가져오므로
    안 펼친 디렉터리의 파일은 클라이언트에 **존재하지도 않는다.**"""
    c, _ws = client_ws
    pid = await _create_product(c)
    await _commit_files(pid, {"src/util/io.py": "y = 2\n", "README.md": "# hi\n"})

    r = await c.get(f"/api/v1/products/{pid}/files/search", params={"q": "io"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert [e["path"] for e in body["results"]] == ["src/util/io.py"]
    assert body["truncated"] is False


async def test_files_search_says_when_it_truncated(client_ws) -> None:
    """상한에 걸린 것을 안 알리면, 화면은 '이게 전부'라고 **거짓말한다**."""
    c, _ws = client_ws
    pid = await _create_product(c)
    await _commit_files(pid, {f"mod{i}.py": "x\n" for i in range(8)})

    r = await c.get(f"/api/v1/products/{pid}/files/search", params={"q": "mod", "limit": 3})
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["results"]) == 3
    assert body["truncated"] is True


async def test_files_search_empty_query_returns_nothing(client_ws) -> None:
    c, _ws = client_ws
    pid = await _create_product(c)
    await _commit_files(pid, {"a.py": "1\n", "b.py": "2\n"})

    r = await c.get(f"/api/v1/products/{pid}/files/search", params={"q": "  "})
    assert r.status_code == 200, r.text
    assert r.json() == {"results": [], "truncated": False}


async def test_files_search_cross_workspace_404(client_ws) -> None:
    """나열·내용과 **같은 게이트**를 지나야 한다. 새 엔드포인트가 조용히 게이트
    밖에 서는 것이 이 레포가 반복해서 밟은 모양이다."""
    c, _ws = client_ws
    r = await c.get(f"/api/v1/products/{uuid.uuid4()}/files/search", params={"q": "a"})
    assert r.status_code == 404


async def test_files_search_does_not_claim_truncation_on_an_exact_fit(client_ws) -> None:
    """정확히 `limit` 개가 매칭되고 **더는 없을 때** 잘렸다고 하면 안 된다.

    개수가 상한과 같은지로 판정하면 이 경우를 구분할 수 없다 — 우연히 같은 것과
    실제로 잘린 것이 같은 숫자를 만든다.
    """
    c, _ws = client_ws
    pid = await _create_product(c)
    await _commit_files(pid, {f"mod{i}.py": "x\n" for i in range(3)})

    r = await c.get(f"/api/v1/products/{pid}/files/search", params={"q": "mod", "limit": 3})
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["results"]) == 3
    assert body["truncated"] is False


async def test_files_search_reports_truncation_even_when_limit_exceeds_the_server_cap(
    client_ws,
) -> None:
    """`limit` 을 서버 상한보다 크게 주면 서버 상한이 이긴다 — 그때도 잘린 것은
    잘렸다고 해야 한다. 개수를 `limit` 과 비교하면 이 경우가 조용히 False 가 된다."""
    c, _ws = client_ws
    pid = await _create_product(c)
    await _commit_files(pid, {f"mod{i:03d}.py": "x\n" for i in range(55)})

    r = await c.get(f"/api/v1/products/{pid}/files/search", params={"q": "mod", "limit": 500})
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["results"]) == 50  # 서버 상한
    assert body["truncated"] is True
