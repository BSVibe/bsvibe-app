"""Tests for StorageBackend implementations."""

from __future__ import annotations

import pytest

from backend.knowledge.graph.storage import FileSystemStorage


@pytest.fixture
def storage(tmp_path):
    return FileSystemStorage(tmp_path)


class TestFileSystemStorage:
    async def test_write_and_read(self, storage, tmp_path):
        await storage.write("notes/hello.md", "# Hello\nWorld")
        content = await storage.read("notes/hello.md")
        assert content == "# Hello\nWorld"
        assert (tmp_path / "notes" / "hello.md").exists()

    async def test_exists(self, storage):
        assert not await storage.exists("missing.md")
        await storage.write("test.md", "content")
        assert await storage.exists("test.md")

    async def test_delete(self, storage):
        await storage.write("test.md", "content")
        await storage.delete("test.md")
        assert not await storage.exists("test.md")

    async def test_delete_missing(self, storage):
        # Should not raise
        await storage.delete("nonexistent.md")

    async def test_list_files(self, storage):
        await storage.write("ideas/a.md", "idea a")
        await storage.write("ideas/b.md", "idea b")
        await storage.write("ideas/c.txt", "not md")
        files = await storage.list_files("ideas")
        assert sorted(files) == ["ideas/a.md", "ideas/b.md"]

    async def test_list_files_empty_dir(self, storage):
        files = await storage.list_files("nonexistent")
        assert files == []

    async def test_content_hash(self, storage):
        await storage.write("test.md", "hello")
        h1 = await storage.content_hash("test.md")
        assert isinstance(h1, str)
        assert len(h1) == 64  # SHA256 hex

        # Same content, same hash
        h2 = await storage.content_hash("test.md")
        assert h1 == h2

        # Different content, different hash
        await storage.write("test.md", "world")
        h3 = await storage.content_hash("test.md")
        assert h3 != h1

    async def test_content_hash_includes_path(self, storage):
        await storage.write("a.md", "same content")
        await storage.write("b.md", "same content")
        h_a = await storage.content_hash("a.md")
        h_b = await storage.content_hash("b.md")
        assert h_a != h_b  # Different paths → different hashes

    async def test_path_traversal_blocked(self, storage):
        with pytest.raises(ValueError, match="escapes storage root"):
            await storage.read("../../etc/passwd")

    async def test_path_traversal_write_blocked(self, storage):
        with pytest.raises(ValueError, match="escapes storage root"):
            await storage.write("../outside.md", "bad")
