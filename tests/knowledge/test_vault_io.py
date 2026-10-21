"""#929 (B), stage 2 PR 2a — every vault read and write goes through its storage.

A note's identity stays what it was — an absolute ``Path`` under ``vault.root`` — so the code
that computes ``relative_to(vault.root)`` or ``resolve_path(...)`` does not change. What changes
is the I/O: existence, reading, writing, appending, listing and deleting are :class:`Vault`
methods that delegate to its :class:`StorageBackend`. On files that is the disk it always was;
on rows ``vault.root`` is only a name and nothing touches the disk.

See docs/design/stateless-workers.md §5.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.knowledge._internal.exceptions import VaultPathError
from backend.knowledge.graph.storage import FileSystemStorage
from backend.knowledge.graph.vault import Vault
from backend.knowledge.infrastructure.document_store import PgDocumentStorage

from .._support import db_engine


@pytest_asyncio.fixture
async def sf() -> AsyncIterator[Any]:
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


@pytest_asyncio.fixture(params=["files", "rows"])
async def vault(request, tmp_path: Path, sf) -> Vault:
    root = tmp_path / "vault" / "us-1" / "ws"
    if request.param == "files":
        return Vault(root)
    return Vault(root, storage=PgDocumentStorage(sf, workspace_id=uuid.uuid4(), tree="vault"))


async def test_a_note_written_by_path_reads_back(vault) -> None:
    note = vault.resolve_path("garden/seedling/a.md")

    await vault.write(note, "# A\n")

    assert await vault.exists(note)
    assert await vault.read(note) == "# A\n"
    assert await vault.read_note_content(note) == "# A\n", "the old reader agrees"


async def test_a_relative_path_names_the_same_note(vault) -> None:
    await vault.write("garden/seedling/a.md", "x")

    assert await vault.exists(vault.root / "garden" / "seedling" / "a.md")


async def test_an_append_creates_then_extends(vault) -> None:
    log = vault.resolve_path("actions/2026-10-21.md")

    await vault.append(log, "- one\n", header="# Actions — 2026-10-21\n\n")
    await vault.append(log, "- two\n", header="# Actions — 2026-10-21\n\n")

    assert await vault.read(log) == "# Actions — 2026-10-21\n\n- one\n- two\n"


async def test_listing_returns_note_paths_under_the_root(vault) -> None:
    for rel in ("garden/seedling/b.md", "garden/seedling/a.md", "garden/seedling/deep/c.md"):
        await vault.write(rel, rel)

    flat = await vault.read_notes("garden/seedling")
    deep = await vault.read_notes("garden/seedling", recursive=True)

    assert flat == [vault.root / "garden/seedling/a.md", vault.root / "garden/seedling/b.md"]
    assert deep == [
        vault.root / "garden/seedling/a.md",
        vault.root / "garden/seedling/b.md",
        vault.root / "garden/seedling/deep/c.md",
    ]
    assert await vault.read_notes("nowhere") == []


async def test_a_note_can_be_moved_and_deleted(vault) -> None:
    src, dst = vault.resolve_path("garden/seedling/a.md"), vault.resolve_path("garden/budding/a.md")
    await vault.write(src, "a")

    await vault.move(src, dst)

    assert not await vault.exists(src) and await vault.read(dst) == "a"
    await vault.delete(dst)
    assert not await vault.exists(dst)


async def test_a_subtree_can_be_cleared(vault) -> None:
    await vault.write("garden/seedling/a.md", "a")
    await vault.write("concepts/active/c.md", "c")

    await vault.delete_tree("garden")

    assert await vault.read_notes("garden/seedling") == []
    assert await vault.exists("concepts/active/c.md")


async def test_a_path_outside_the_vault_is_refused(vault, tmp_path: Path) -> None:
    with pytest.raises(VaultPathError):
        await vault.write(tmp_path / "elsewhere.md", "x")
    with pytest.raises(VaultPathError):
        await vault.read("../escape.md")


async def test_rows_never_touch_the_disk(tmp_path: Path, sf) -> None:
    root = tmp_path / "vault"
    vault = Vault(root, storage=PgDocumentStorage(sf, workspace_id=uuid.uuid4(), tree="vault"))

    vault.ensure_dirs()
    await vault.write("garden/seedling/a.md", "a")
    await vault.append("actions/log.md", "- x\n", header="# Log\n")

    assert not root.exists(), "on rows the root is a name, not a directory"


def test_files_stay_the_default(tmp_path: Path) -> None:
    vault = Vault(tmp_path)

    assert isinstance(vault.storage, FileSystemStorage)
    assert vault.storage.root == tmp_path.resolve()


# --- the garden writer, on either store -----------------------------------------------------


async def test_the_garden_writer_works_the_same_on_either_store(vault) -> None:
    """Seeds, garden notes (with dedup), entity stubs, logs, edits, tombstones, promotion."""
    from backend.knowledge.graph.writer_core import GardenNote, GardenWriter

    writer = GardenWriter(vault=vault)

    seed = await writer.write_seed("mcp", {"title": "S", "content": "seed body"})
    first = await writer.write_garden(GardenNote(title="Idea", content="one", source="t"))
    second = await writer.write_garden(GardenNote(title="Idea", content="two", source="t"))
    stub = await writer.ensure_entity_stub("Alice", first)
    await writer.ensure_entity_stub("Alice", second)
    await writer.write_action("skill", "did a thing")
    await writer.write_action("skill", "did another")
    await writer.append_to_note(writer_rel(vault, first), "\nmore\n")
    await writer.update_note(writer_rel(vault, second), "rewritten\n")
    await writer.tombstone_note(
        writer_rel(vault, first), retracted_at="2026-10-21T00:00:00Z", retracted_by="u"
    )

    assert seed.is_relative_to(vault.root) and "seed body" in await vault.read(seed)
    assert first.name == "idea.md" and second.name == "idea_001.md", "dedup sees the store"
    assert (await vault.read(first)).rstrip().endswith("more")
    assert "retracted_at" in await vault.read(first)
    assert (await vault.read(second)).rstrip().endswith("rewritten")
    stub_text = await vault.read(stub)
    assert "idea.md" in stub_text and "idea_001.md" in stub_text, "both mentions recorded"
    actions = await vault.read_notes("actions")
    assert len(actions) == 1 and (await vault.read(actions[0])).count("| `skill` |") == 2

    await writer.delete_note(writer_rel(vault, second))
    assert not await vault.exists(second)


def writer_rel(vault: Vault, path: Path) -> str:
    return path.relative_to(vault.root).as_posix()
