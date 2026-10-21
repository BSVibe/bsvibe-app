"""Vault — secure path management and file access for the 2nd Brain.

A note is named by an absolute ``Path`` under :attr:`Vault.root`; that does not change with the
store. Every read and write goes through the vault's :class:`StorageBackend` (#929 stage 2):
files on disk by default, or ``vault_documents`` rows, where ``root`` is only a name.
"""

from __future__ import annotations

import asyncio
from contextlib import AbstractAsyncContextManager
from pathlib import Path

import structlog

from backend.knowledge._internal.exceptions import VaultPathError
from backend.knowledge.graph.storage import FileSystemStorage, StorageBackend

logger = structlog.get_logger(__name__)

VAULT_SUBDIRS = (
    # Legacy v2.2 entity-type folders (preserved during the canonicalization
    # transition; flatten migration moves notes into garden/<maturity>/).
    "ideas",
    "insights",
    "projects",
    "people",
    "events",
    "tasks",
    "facts",
    "preferences",
    "seeds",
    # Canonicalization layout (Handoff §1).
    "raw",
    "garden/seedling",
    "garden/budding",
    "garden/evergreen",
    "concepts/active",
    "concepts/merged",
    "concepts/deprecated",
    "proposals",
    "actions",
    "decisions",
    ".bsage",
)


class Vault:
    """Manages the on-disk vault directory structure and enforces path boundaries.

    v2.2 vault structure — each entity type maps to a top-level folder.
    System metadata lives in ``.bsage/``.
    """

    def __init__(self, vault_path: Path, *, storage: StorageBackend | None = None) -> None:
        self._root = vault_path.resolve()
        self._storage = storage if storage is not None else FileSystemStorage(self._root)

    @property
    def root(self) -> Path:
        """Return the resolved vault root path."""
        return self._root

    @property
    def storage(self) -> StorageBackend:
        """Where the notes actually live."""
        return self._storage

    @property
    def _on_disk(self) -> bool:
        return isinstance(self._storage, FileSystemStorage)

    def ensure_dirs(self) -> None:
        """Create seeds/, garden/, actions/ subdirectories if they don't exist.

        Rows have no directories: there it does nothing."""
        if not isinstance(self._storage, FileSystemStorage):
            return
        for subdir in VAULT_SUBDIRS:
            path = self._storage.root / subdir
            path.mkdir(parents=True, exist_ok=True)
            logger.debug("vault_dir_ensured", path=str(path))

    def resolve_path(self, subpath: str) -> Path:
        """Resolve a subpath within the vault, blocking directory traversal.

        Args:
            subpath: Relative path within the vault (e.g. "seeds/calendar/2026-02-21.md").

        Returns:
            Resolved absolute Path within the vault.

        Raises:
            VaultPathError: If the resolved path escapes the vault boundary.
        """
        resolved = (self._root / subpath).resolve()
        if not resolved.is_relative_to(self._root):
            logger.warning(
                "vault_path_traversal_blocked",
                subpath=subpath,
                resolved=str(resolved),
            )
            raise VaultPathError(f"Path traversal detected: '{subpath}' resolves outside the vault")
        return resolved

    def rel(self, path: Path | str) -> str:
        """The vault-relative POSIX path of ``path`` (absolute under the root, or relative)."""
        resolved = self.resolve_path(str(path)) if not Path(path).is_absolute() else Path(path)
        resolved = resolved.resolve() if self._on_disk else resolved
        if not resolved.is_relative_to(self._root):
            raise VaultPathError(f"Path traversal detected: '{path}' resolves outside the vault")
        return resolved.relative_to(self._root).as_posix()

    def lock(self, name: str) -> AbstractAsyncContextManager[None]:
        """Exclude every other writer of this vault holding ``name`` — in this process, on
        this box, and (on rows) on every machine. See :meth:`StorageBackend.lock`."""
        return self._storage.lock(name)

    async def read(self, path: Path | str) -> str:
        return await self._storage.read(self.rel(path))

    async def write(self, path: Path | str, content: str) -> None:
        await self._storage.write(self.rel(path), content)

    async def append(self, path: Path | str, text: str, *, header: str = "") -> None:
        """Append ``text``; a note that does not exist yet starts with ``header``."""
        rel = self.rel(path)
        if isinstance(self._storage, FileSystemStorage):
            target = self._storage.root / rel

            def _append() -> None:
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    with target.open("a", encoding="utf-8") as f:
                        f.write(text)
                else:
                    target.write_text(header + text, encoding="utf-8")

            await asyncio.to_thread(_append)
            return
        try:
            current = await self._storage.read(rel)
        except FileNotFoundError:
            current = header
        await self._storage.write(rel, current + text)

    async def create(self, path: Path | str, content: str) -> bool:
        """Write only if absent, atomically — ``False`` when it already existed."""
        return await self._storage.create(self.rel(path), content)

    async def list_dirs(self, subdir: str) -> list[str]:
        """Names of the directories directly under ``subdir`` that hold any file."""
        base = self.rel(subdir) if subdir else ""
        depth = len(base.split("/")) if base else 0
        names = {
            r.split("/")[depth]
            for r in await self._storage.list_files(base, "*")
            if len(r.split("/")) > depth + 1
        }
        return sorted(names)

    async def exists(self, path: Path | str) -> bool:
        return await self._storage.exists(self.rel(path))

    async def delete(self, path: Path | str) -> None:
        await self._storage.delete(self.rel(path))

    async def delete_tree(self, subdir: str) -> None:
        await self._storage.delete_prefix(self.rel(subdir) if subdir else "")

    async def move(self, src: Path | str, dst: Path | str) -> None:
        content = await self.read(src)
        await self.write(dst, content)
        await self.delete(src)

    async def read_notes(self, subdir: str, *, recursive: bool = False) -> list[Path]:
        """Return sorted list of .md files in a vault subdirectory.

        Args:
            subdir: Relative directory path within the vault (e.g. "garden/ideas").
                An empty string means the vault root.
            recursive: When True, walk the entire subtree rooted at ``subdir``.
                When False (the historical default), only direct children of
                ``subdir`` are returned. The MCP knowledge tools opt into True at
                their boundary so the founder sees the whole ingest output (Lift E10).

        Returns:
            Absolute note paths under :attr:`root` — sorted by vault-relative path
            when recursive, by filename otherwise. Empty if nothing is there.
        """
        base = self.rel(subdir) if subdir else ""
        rels = await self._storage.list_files(base, "*.md")
        prefix_depth = len(base.split("/")) if base else 0
        if not recursive:
            rels = [r for r in rels if len(r.split("/")) == prefix_depth + 1]
            paths = sorted((self._root / r for r in rels), key=lambda p: p.name)
        else:
            paths = sorted((self._root / r for r in rels), key=lambda p: p.as_posix())
        logger.debug("vault_read_notes", subdir=subdir, count=len(paths))
        return paths

    async def read_note_content(self, path: Path) -> str:
        """Read the text content of a note.

        Raises:
            VaultPathError: If the path is outside the vault boundary.
            FileNotFoundError: If there is no such note.
        """
        return await self.read(path)
