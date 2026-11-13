"""Vault filesystem scanner.

Walks an Obsidian vault root, yields markdown files whose relative path does
not match any exclude glob. Read-only. The scanner does NOT parse frontmatter —
that's :mod:`plugin.obsidian.parser`'s job.

Safety properties (the vault path is founder/agent-supplied, the host is the server):

* Symlinks are never followed — directories are tested with
  ``is_dir(follow_symlinks=False)`` and files are opened with ``O_NOFOLLOW``.
* Every visited entry is resolved and must live under the allowed root; an entry that
  resolves outside is skipped and reported in :attr:`VaultScanner.skipped_outside`.
* Limits are enforced WHILE walking (note count, visited-entry count, bytes actually
  read). Hitting one stops the scan and records :attr:`VaultScanner.stop_reason`.
* Excluded directories are not descended into.
"""

from __future__ import annotations

import fnmatch
import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

DEFAULT_EXCLUDE_PATTERNS: tuple[str, ...] = (
    ".obsidian/**",
    "Templates/**",
)

DEFAULT_MAX_NOTES = 5_000
DEFAULT_MAX_ENTRIES = 200_000
DEFAULT_MAX_BYTES = 256 * 1024 * 1024


@dataclass(frozen=True)
class ScannedNote:
    """One markdown note found in the vault.

    ``relative_path`` is POSIX-style relative to the vault root. ``text`` is the raw
    file contents (UTF-8, errors replaced).
    """

    relative_path: str
    text: str


class VaultScanner:
    """Walk an Obsidian vault directory and yield :class:`ScannedNote`."""

    def __init__(
        self,
        root: Path,
        exclude_patterns: tuple[str, ...] | list[str] | None = None,
        *,
        allowed_root: Path | None = None,
        max_notes: int = DEFAULT_MAX_NOTES,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> None:
        self._root = Path(root)
        self._allowed_root = Path(allowed_root) if allowed_root is not None else None
        self._max_notes = max_notes
        self._max_entries = max_entries
        self._max_bytes = max_bytes
        # ``None`` → defaults; ``[]`` → caller explicitly wants no excludes.
        if exclude_patterns is None:
            self._exclude_patterns: tuple[str, ...] = DEFAULT_EXCLUDE_PATTERNS
        else:
            self._exclude_patterns = tuple(exclude_patterns)
        self.skipped_outside: list[str] = []
        self.skipped_symlinks: int = 0
        self.entries_visited: int = 0
        self.bytes_read: int = 0
        self.stop_reason: str | None = None

    def scan(self) -> Iterator[ScannedNote]:
        """Yield every non-excluded ``*.md`` note under the vault root.

        Raises :class:`FileNotFoundError` when the root is missing and
        :class:`NotADirectoryError` when the root is a file.
        """
        root, boundary = self._prepare_root()

        notes = 0
        stack: list[Path] = [root]
        while stack:
            directory = stack.pop()
            entries = _list_dir(directory)
            subdirs: list[Path] = []
            for entry in entries:
                self.entries_visited += 1
                if self.entries_visited > self._max_entries:
                    self.stop_reason = f"entry limit exceeded ({self._max_entries})"
                    return
                path = Path(entry.path)
                rel = path.relative_to(root).as_posix()
                if not _resolves_within(path, boundary):
                    self.skipped_outside.append(rel)
                    continue
                if entry.is_symlink():
                    # Never followed, even when the target is inside the root.
                    self.skipped_symlinks += 1
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if not self._is_excluded(rel + "/anything"):
                        subdirs.append(path)
                    continue
                if not entry.is_file(follow_symlinks=False) or not entry.name.endswith(".md"):
                    continue
                if self._is_excluded(rel):
                    continue
                if notes >= self._max_notes:
                    self.stop_reason = f"note limit exceeded ({self._max_notes})"
                    return
                text = self._read(path)
                if text is None:
                    if self.stop_reason is not None:
                        return
                    continue
                notes += 1
                yield ScannedNote(relative_path=rel, text=text)
            # Reverse so the stack pops in sorted order.
            stack.extend(reversed(subdirs))

    def _prepare_root(self) -> tuple[Path, Path]:
        if not self._root.exists():
            raise FileNotFoundError(f"obsidian: vault root not found: {self._root}")
        if not self._root.is_dir():
            raise NotADirectoryError(f"obsidian: vault root is not a directory: {self._root}")
        root = self._root.resolve()
        boundary = self._allowed_root.resolve() if self._allowed_root is not None else root
        if not _is_within(root, boundary):
            raise PermissionError(f"obsidian: vault root is outside the allowed root: {self._root}")
        return root, boundary

    def _read(self, path: Path) -> str | None:
        """Read ``path`` without following a symlink, counting bytes actually read."""
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        try:
            fd = os.open(path, flags)
        except OSError:
            self.skipped_symlinks += 1
            return None
        remaining = self._max_bytes - self.bytes_read
        with os.fdopen(fd, "rb") as fh:
            data = fh.read(remaining + 1)
        if len(data) > remaining:
            self.bytes_read += remaining
            self.stop_reason = f"byte limit exceeded ({self._max_bytes})"
            return None
        self.bytes_read += len(data)
        return data.decode("utf-8", errors="replace")

    def _is_excluded(self, relative_path: str) -> bool:
        """Match ``relative_path`` against every exclude pattern.

        Each parent directory is probed too so ``.obsidian/**`` excludes
        ``.obsidian/plugins/foo/data.md`` under fnmatch semantics.
        """
        candidates = [relative_path]
        parts = relative_path.split("/")
        for i in range(1, len(parts)):
            candidates.append("/".join(parts[:i]) + "/" + "anything")
        for pattern in self._exclude_patterns:
            for candidate in candidates:
                if fnmatch.fnmatchcase(candidate, pattern):
                    return True
        return False


def _list_dir(directory: Path) -> list[os.DirEntry[str]]:
    try:
        with os.scandir(directory) as it:
            return sorted(it, key=lambda e: e.name)
    except OSError:
        return []


def _resolves_within(path: Path, boundary: Path) -> bool:
    try:
        return _is_within(path.resolve(), boundary)
    except (OSError, RuntimeError):
        return False


def _is_within(path: Path, boundary: Path) -> bool:
    return path == boundary or boundary in path.parents


__all__ = [
    "DEFAULT_EXCLUDE_PATTERNS",
    "DEFAULT_MAX_BYTES",
    "DEFAULT_MAX_ENTRIES",
    "DEFAULT_MAX_NOTES",
    "ScannedNote",
    "VaultScanner",
]
