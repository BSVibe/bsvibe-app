"""#929 (B), stage 2 PR 2b — one place decides where a workspace's vault and skills live.

``knowledge_store_backend`` picks the store: ``filesystem`` (the default, today's files) or
``postgres`` (``vault_documents`` rows). Every caller asks the same factory, so flipping the
setting moves the API, the workers and the MCP tools at once — no caller builds its own
``FileSystemStorage(root)`` or ``Vault(root)``. docs/design/stateless-workers.md §5.
"""

from __future__ import annotations

import ast
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.config import get_settings
from backend.knowledge import vault_store
from backend.knowledge.graph.storage import FileSystemStorage
from backend.knowledge.graph.vault_paths import workspace_vault_root
from backend.knowledge.infrastructure.document_store import PgDocumentStorage

from .._support import db_engine

_REPO = Path(__file__).resolve().parents[2]


@pytest_asyncio.fixture
async def sf() -> AsyncIterator[Any]:
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def on(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, sf):
    def _set(backend: str) -> None:
        s = get_settings()
        monkeypatch.setattr(s, "knowledge_store_backend", backend, raising=False)
        monkeypatch.setattr(s, "knowledge_vault_root", str(tmp_path / "vault"), raising=False)
        monkeypatch.setattr(s, "skills_root", str(tmp_path / "skills"), raising=False)
        monkeypatch.setattr(vault_store, "_session_factory", lambda: sf)

    return _set


def test_files_are_the_default() -> None:
    assert get_settings().knowledge_store_backend == "filesystem"


def test_on_files_the_vault_is_the_directory_it_always_was(on) -> None:
    on("filesystem")
    ws = uuid.uuid4()

    vault = vault_store.workspace_vault(ws)

    assert vault.root == workspace_vault_root(ws).resolve()
    assert isinstance(vault.storage, FileSystemStorage)
    assert vault.storage.root == vault.root
    skills = vault_store.workspace_skills_storage(ws)
    assert isinstance(skills, FileSystemStorage)
    assert skills.root == (Path(get_settings().skills_root) / str(ws)).resolve()


async def test_on_postgres_both_trees_are_rows_of_that_workspace(on) -> None:
    on("postgres")
    ws = uuid.uuid4()

    vault = vault_store.workspace_vault(ws)
    skills = vault_store.workspace_skills_storage(ws)
    await vault.write("garden/a.md", "note")
    await skills.write("s.md", "skill")

    assert isinstance(vault.storage, PgDocumentStorage) and isinstance(skills, PgDocumentStorage)
    assert vault.root == workspace_vault_root(ws).resolve(), "the same names on either store"
    assert await vault_store.workspace_vault_storage(ws).read("garden/a.md") == "note"
    assert not await vault_store.workspace_vault_storage(ws).exists("s.md"), "separate trees"
    assert not Path(get_settings().knowledge_vault_root).exists(), "nothing on disk"


def test_an_unknown_backend_fails_loudly(on) -> None:
    on("nfs")

    with pytest.raises(ValueError, match="knowledge_store_backend"):
        vault_store.workspace_vault(uuid.uuid4())


def _constructions(tree: ast.AST) -> list[int]:
    """Lines that build a vault or a vault-shaped store by hand."""
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            name = fn.id if isinstance(fn, ast.Name) else fn.attr if isinstance(fn, ast.Attribute) else ""
            if name in {"Vault", "_Vault", "FileSystemStorage"}:
                hits.append(node.lineno)
    return hits


#: The modules that legitimately construct one: the store's own definition and the factory.
_ALLOWED = {
    "backend/knowledge/graph/vault.py",
    "backend/knowledge/graph/storage.py",
    "backend/knowledge/vault_store.py",
}


def test_no_caller_builds_its_own_vault_store() -> None:
    offenders = []
    for path in sorted((_REPO / "backend").rglob("*.py")):
        rel = path.relative_to(_REPO).as_posix()
        if rel in _ALLOWED or "/migrations/" in rel:
            continue
        lines = _constructions(ast.parse(path.read_text(encoding="utf-8")))
        offenders += [f"{rel}:{n}" for n in lines]

    assert offenders == [], "build it through backend.knowledge.vault_store:\n" + "\n".join(offenders)
