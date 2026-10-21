"""StorageBackend — abstraction for markdown file storage.

Two backends: the local filesystem (:class:`FileSystemStorage`) and Postgres rows
(:class:`backend.knowledge.infrastructure.document_store.PgDocumentStorage`, #929 stage 2), so
several machines can share one vault. Both hold the same Markdown at the same relative paths.
"""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import shutil
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import IO, Final


class _ProcessLocks:
    """One ``asyncio.Lock`` per (event loop, key) for the whole process.

    A lock created per writer instance — the old shape — excludes nothing: an instance is
    made per request. Keyed by the loop too, because an ``asyncio.Lock`` belongs to one."""

    def __init__(self) -> None:
        self._locks: dict[tuple[int, str, str], asyncio.Lock] = {}

    def get(self, scope: str, name: str) -> asyncio.Lock:
        key = (id(asyncio.get_running_loop()), scope, name)
        lock = self._locks.get(key)
        if lock is None:
            lock = self._locks[key] = asyncio.Lock()
        return lock


_PROCESS_LOCKS: Final[_ProcessLocks] = _ProcessLocks()


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

    @asynccontextmanager
    async def lock(self, name: str) -> AsyncIterator[None]:
        """Exclude every other holder of ``name`` on this tree while the block runs.

        For read-modify-write sections (an append, a frontmatter edit). The default holds
        within this process; the stores extend it across processes (files) and machines
        (rows). Not reentrant: never take the same name twice in one task."""
        async with _PROCESS_LOCKS.get(self._lock_scope(), name):
            yield

    def _lock_scope(self) -> str:
        return f"{type(self).__name__}:{id(self)}"


class FileSystemStorage(StorageBackend):
    """Local filesystem storage backend."""

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    @property
    def root(self) -> Path:
        return self._root

    def _lock_scope(self) -> str:
        return f"files:{self._root}"

    @asynccontextmanager
    async def lock(self, name: str) -> AsyncIterator[None]:
        """Within the process, then across processes on this box (``flock`` on a file next to
        the tree — never inside it, so it is never mistaken for a document)."""
        async with _PROCESS_LOCKS.get(self._lock_scope(), name):
            lock_dir = self._root.parent / ".locks"
            safe = hashlib.sha256(f"{self._root.name}:{name}".encode()).hexdigest()[:32]

            def _acquire() -> IO[str]:
                lock_dir.mkdir(parents=True, exist_ok=True)
                handle = (lock_dir / f"{safe}.lock").open("a+")
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                return handle

            def _release(handle: IO[str]) -> None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                handle.close()

            handle = await asyncio.to_thread(_acquire)
            try:
                yield
            finally:
                await asyncio.to_thread(_release, handle)

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
