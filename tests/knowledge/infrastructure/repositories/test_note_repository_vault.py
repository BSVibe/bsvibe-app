"""Lift I-Repo-Knowledge — VaultNoteRepository round-trip tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.knowledge.graph.storage import FileSystemStorage
from backend.knowledge.infrastructure.repositories import VaultNoteRepository


@pytest.fixture
def vault_repo(tmp_path: Path) -> VaultNoteRepository:
    storage = FileSystemStorage(tmp_path)
    return VaultNoteRepository(storage)


@pytest.mark.asyncio
async def test_write_then_read_roundtrip(vault_repo: VaultNoteRepository) -> None:
    await vault_repo.write("notes/hello.md", "# Hello\n\nbody.")
    body = await vault_repo.read("notes/hello.md")
    assert body == "# Hello\n\nbody."


@pytest.mark.asyncio
async def test_exists_reflects_state(vault_repo: VaultNoteRepository) -> None:
    assert not await vault_repo.exists("missing.md")
    await vault_repo.write("present.md", "x")
    assert await vault_repo.exists("present.md")


@pytest.mark.asyncio
async def test_list_paths_returns_md_files(vault_repo: VaultNoteRepository) -> None:
    await vault_repo.write("dir/a.md", "a")
    await vault_repo.write("dir/b.md", "b")
    await vault_repo.write("dir/skip.txt", "skip")
    paths = await vault_repo.list_paths("dir")
    # FileSystemStorage.list_files default pattern is *.md
    names = {Path(p).name for p in paths}
    assert names == {"a.md", "b.md"}


@pytest.mark.asyncio
async def test_delete_is_idempotent(vault_repo: VaultNoteRepository) -> None:
    await vault_repo.write("ephemeral.md", "x")
    assert await vault_repo.exists("ephemeral.md")
    await vault_repo.delete("ephemeral.md")
    assert not await vault_repo.exists("ephemeral.md")
    # Second delete must not raise.
    await vault_repo.delete("ephemeral.md")
