"""Vault — secure path management and file access for the 2nd Brain."""

from __future__ import annotations

import asyncio
from pathlib import Path

import structlog

from backend.knowledge._internal.exceptions import VaultPathError

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

    def __init__(self, vault_path: Path) -> None:
        self._root = vault_path.resolve()

    @property
    def root(self) -> Path:
        """Return the resolved vault root path."""
        return self._root

    def ensure_dirs(self) -> None:
        """Create seeds/, garden/, actions/ subdirectories if they don't exist."""
        for subdir in VAULT_SUBDIRS:
            path = self._root / subdir
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

    async def read_notes(self, subdir: str) -> list[Path]:
        """Return sorted list of .md files in a vault subdirectory.

        Args:
            subdir: Relative directory path within the vault (e.g. "garden/ideas").

        Returns:
            List of Path objects for .md files, sorted by filename.
            Returns an empty list if the directory doesn't exist.
        """
        target = self.resolve_path(subdir)

        def _read() -> tuple[list[Path], bool]:
            if not target.is_dir():
                return [], False
            return sorted(target.glob("*.md"), key=lambda p: p.name), True

        md_files, dir_exists = await asyncio.to_thread(_read)
        if md_files:
            logger.debug("vault_read_notes", subdir=subdir, count=len(md_files))
        elif dir_exists:
            logger.debug("vault_read_notes_empty", subdir=subdir)
        else:
            logger.debug("vault_read_notes_no_dir", subdir=subdir)
        return md_files

    async def read_note_content(self, path: Path) -> str:
        """Read the text content of a note file asynchronously.

        The path must be within the vault boundary.

        Args:
            path: Absolute path to the note file.

        Returns:
            The text content of the note.

        Raises:
            VaultPathError: If the path is outside the vault boundary.
            OSError: If the file cannot be read.
        """
        resolved = path.resolve()
        if not resolved.is_relative_to(self._root):
            raise VaultPathError(f"Path traversal detected: '{path}' resolves outside the vault")
        return await asyncio.to_thread(resolved.read_text, encoding="utf-8")
