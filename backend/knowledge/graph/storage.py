"""StorageBackend — abstraction for markdown file storage.

Two backends: the local filesystem (:class:`FileSystemStorage`) and Postgres rows
(:class:`backend.knowledge.infrastructure.document_store.PgDocumentStorage`, #929 stage 2), so
several machines can share one vault. Both hold the same Markdown at the same relative paths.
"""

from __future__ import annotations

import asyncio
import hashlib
import shutil
from abc import ABC, abstractmethod
from pathlib import Path


class StorageBackend(ABC):
    """Abstract interface for reading/writing markdown files."""

    @abstractmethod
    async def read(self, rel_path: str) -> str:
        """Read file content by vault-relative path."""

    @abstractmethod
    async def write(self, rel_path: str, content: str) -> None:
        """Write content to a vault-relative path. Creates parent dirs."""

    @abstractmethod
    async def create(self, rel_path: str, content: str) -> bool:
        """Write only if absent, atomically. ``False`` when it already existed (untouched)."""

    @abstractmethod
    async def delete(self, rel_path: str) -> None:
        """Delete a file by vault-relative path."""

    @abstractmethod
    async def exists(self, rel_path: str) -> bool:
        """Check if a file exists."""

    @abstractmethod
    async def list_files(self, subdir: str, pattern: str = "*.md") -> list[str]:
        """List files matching pattern in a subdirectory. Returns relative paths."""

    @abstractmethod
    async def content_hash(self, rel_path: str) -> str:
        """Return SHA256 hash of file content + relative path."""

    async def read_many(self, rel_paths: list[str]) -> dict[str, str]:
        """Read several files at once; a missing one is left out of the result."""
        found: dict[str, str] = {}
        for rel_path in rel_paths:
            try:
                found[rel_path] = await self.read(rel_path)
            except FileNotFoundError:
                continue
        return found

    @abstractmethod
    async def delete_prefix(self, subdir: str) -> None:
        """Delete every file under a subdirectory (``""`` = the whole tree)."""


class FileSystemStorage(StorageBackend):
    """Local filesystem storage backend."""

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    @property
    def root(self) -> Path:
        return self._root

    def _resolve(self, rel_path: str) -> Path:
        """Resolve and validate a vault-relative path (prevent traversal)."""
        resolved = (self._root / rel_path).resolve()
        if not str(resolved).startswith(str(self._root)):
            msg = f"Path escapes storage root: {rel_path}"
            raise ValueError(msg)
        return resolved

    async def read(self, rel_path: str) -> str:
        path = self._resolve(rel_path)
        return await asyncio.to_thread(path.read_text, encoding="utf-8")

    async def write(self, rel_path: str, content: str) -> None:
        path = self._resolve(rel_path)
        await asyncio.to_thread(path.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(path.write_text, content, encoding="utf-8")

    async def create(self, rel_path: str, content: str) -> bool:
        path = self._resolve(rel_path)

        def _create() -> bool:
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                with path.open("x", encoding="utf-8") as handle:
                    handle.write(content)
            except FileExistsError:
                return False
            return True

        return await asyncio.to_thread(_create)

    async def delete(self, rel_path: str) -> None:
        path = self._resolve(rel_path)
        await asyncio.to_thread(path.unlink, missing_ok=True)

    async def exists(self, rel_path: str) -> bool:
        path = self._resolve(rel_path)
        return await asyncio.to_thread(path.exists)

    async def list_files(self, subdir: str, pattern: str = "*.md") -> list[str]:
        base = self._resolve(subdir)
        if not await asyncio.to_thread(base.is_dir):
            return []

        def _scan() -> list[str]:
            return sorted(
                str(p.relative_to(self._root)) for p in base.rglob(pattern) if p.is_file()
            )

        return await asyncio.to_thread(_scan)

    async def delete_prefix(self, subdir: str) -> None:
        base = self._resolve(subdir)
        if base == self._root:

            def _clear() -> None:
                for child in self._root.iterdir():
                    if child.is_dir():
                        shutil.rmtree(child, ignore_errors=True)
                    else:
                        child.unlink(missing_ok=True)

            await asyncio.to_thread(_clear)
            return
        await asyncio.to_thread(shutil.rmtree, base, True)

    async def content_hash(self, rel_path: str) -> str:
        content = await self.read(rel_path)
        payload = content.encode("utf-8") + b"\x00" + rel_path.encode("utf-8")
        return hashlib.sha256(payload).hexdigest()
