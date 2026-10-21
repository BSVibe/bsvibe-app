"""#929 (B), stage 2 PR 1 — the vault and skills can live in Postgres rows.

One contract, two stores: whatever the knowledge code may assume of ``StorageBackend`` holds
for the files on disk (today) and for ``vault_documents`` rows (multi-machine). The Markdown
does not change — a row's ``content`` is exactly what the file would hold, and its ``path`` is
exactly the file's vault-relative path. See docs/design/stateless-workers.md §5.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.knowledge.graph.storage import FileSystemStorage, StorageBackend
from backend.knowledge.infrastructure.document_store import PgDocumentStorage, VaultDocumentRow

from .._support import db_engine


@pytest_asyncio.fixture
async def sf() -> AsyncIterator[Any]:
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


@pytest_asyncio.fixture(params=["files", "rows"])
async def storage(request, tmp_path: Path, sf) -> StorageBackend:
    if request.param == "files":
        return FileSystemStorage(tmp_path)
    return PgDocumentStorage(sf, workspace_id=uuid.uuid4(), tree="vault")


async def test_a_written_document_reads_back_exactly(storage) -> None:
    body = "---\ntitle: Hello\n---\n# Hello\n\nWorld — 한국어도 그대로\n"
    await storage.write("garden/seedling/hello.md", body)

    assert await storage.read("garden/seedling/hello.md") == body
    assert await storage.exists("garden/seedling/hello.md")


async def test_a_rewrite_replaces_the_document(storage) -> None:
    await storage.write("a.md", "v1")
    await storage.write("a.md", "v2")

    assert await storage.read("a.md") == "v2"
    assert await storage.list_files("", "*.md") == ["a.md"]


async def test_a_missing_document_is_not_found(storage) -> None:
    assert not await storage.exists("missing.md")
    with pytest.raises(FileNotFoundError):
        await storage.read("missing.md")


async def test_delete_removes_and_tolerates_missing(storage) -> None:
    await storage.write("test.md", "content")
    await storage.delete("test.md")
    await storage.delete("never-was.md")

    assert not await storage.exists("test.md")


async def test_listing_is_recursive_filtered_and_sorted(storage) -> None:
    for path in ("ideas/b.md", "ideas/a.md", "ideas/deep/c.md", "ideas/c.txt", "other/d.md"):
        await storage.write(path, path)

    assert await storage.list_files("ideas") == ["ideas/a.md", "ideas/b.md", "ideas/deep/c.md"]
    assert await storage.list_files("ideas", "*.txt") == ["ideas/c.txt"]
    assert await storage.list_files("nonexistent") == []


async def test_a_listing_does_not_match_a_sibling_prefix(storage) -> None:
    await storage.write("garden/a.md", "a")
    await storage.write("garden_old/b.md", "b")

    assert await storage.list_files("garden") == ["garden/a.md"]


async def test_wildcard_characters_in_a_directory_are_literal(storage) -> None:
    await storage.write("a_b/x.md", "x")
    await storage.write("axb/y.md", "y")
    await storage.write("100%/z.md", "z")

    assert await storage.list_files("a_b") == ["a_b/x.md"]
    assert await storage.list_files("100%") == ["100%/z.md"]


async def test_the_whole_tree_lists_from_the_root(storage) -> None:
    await storage.write("x.md", "x")
    await storage.write("d/y.md", "y")

    assert await storage.list_files("") == ["d/y.md", "x.md"]


async def test_the_content_hash_is_the_same_formula_on_both_stores(storage) -> None:
    await storage.write("notes/a.md", "same content")
    await storage.write("notes/b.md", "same content")

    expected = hashlib.sha256(b"same content\x00notes/a.md").hexdigest()
    assert await storage.content_hash("notes/a.md") == expected
    assert await storage.content_hash("notes/b.md") != expected, "the path is part of it"


async def test_many_documents_read_in_one_call(storage) -> None:
    await storage.write("a.md", "A")
    await storage.write("d/b.md", "B")

    assert await storage.read_many(["a.md", "d/b.md", "missing.md"]) == {
        "a.md": "A",
        "d/b.md": "B",
    }


async def test_a_subtree_can_be_removed_at_once(storage) -> None:
    for path in ("garden/a.md", "garden/deep/b.md", "garden_old/c.md", "keep.md"):
        await storage.write(path, path)

    await storage.delete_prefix("garden")

    assert await storage.list_files("") == ["garden_old/c.md", "keep.md"]


@pytest.mark.parametrize(
    "bad", ["../../etc/passwd", "../outside.md", "/etc/passwd", "a/../../b.md"]
)
async def test_a_path_cannot_escape_the_tree(storage, bad) -> None:
    with pytest.raises(ValueError, match="escapes storage root"):
        await storage.read(bad)
    with pytest.raises(ValueError, match="escapes storage root"):
        await storage.write(bad, "bad")


async def test_an_equivalent_spelling_is_the_same_document(storage) -> None:
    await storage.write("./garden//a.md", "a")

    assert await storage.read("garden/a.md") == "a"
    assert await storage.list_files("garden") == ["garden/a.md"]


# --- rows only ----------------------------------------------------------------------------------


async def test_each_workspace_sees_only_its_own_documents(sf) -> None:
    a = PgDocumentStorage(sf, workspace_id=uuid.uuid4(), tree="vault")
    b = PgDocumentStorage(sf, workspace_id=uuid.uuid4(), tree="vault")
    await a.write("note.md", "A's")
    await b.write("note.md", "B's")

    assert await a.read("note.md") == "A's" and await b.read("note.md") == "B's"
    await a.delete_prefix("")
    assert await b.list_files("") == ["note.md"]


async def test_the_vault_and_the_skills_are_separate_trees(sf) -> None:
    ws = uuid.uuid4()
    vault = PgDocumentStorage(sf, workspace_id=ws, tree="vault")
    skills = PgDocumentStorage(sf, workspace_id=ws, tree="skills")
    await vault.write("x.md", "note")
    await skills.write("x.md", "skill")

    assert await vault.read("x.md") == "note" and await skills.read("x.md") == "skill"


async def test_a_row_holds_the_file_exactly(sf) -> None:
    """``content`` is the file and ``content_hash`` is the bare content's sha256 — what
    reconcile compares (``note_embeddings.content_hash``)."""
    from sqlalchemy import select

    from backend.data.scoping import workspace_scope

    ws = uuid.uuid4()
    store = PgDocumentStorage(sf, workspace_id=ws, tree="vault")
    await store.write("garden/a.md", "body\n")

    async with sf() as s:
        with workspace_scope(ws):
            row = (await s.execute(select(VaultDocumentRow))).scalar_one()
    assert (row.workspace_id, row.tree, row.path, row.content) == (
        ws,
        "vault",
        "garden/a.md",
        "body\n",
    )
    assert row.content_hash == hashlib.sha256(b"body\n").hexdigest()


def test_the_migration_creates_the_table_the_model_maps() -> None:
    import importlib

    mig = importlib.import_module("backend.data.migrations.versions.20261021_vault_documents")
    assert mig.revision == "vault_documents"
    assert VaultDocumentRow.__tablename__ == "vault_documents"


async def test_create_only_writes_a_missing_document(storage) -> None:
    assert await storage.create("seeds/k.md", "first") is True
    assert await storage.create("seeds/k.md", "second") is False
    assert await storage.read("seeds/k.md") == "first"
