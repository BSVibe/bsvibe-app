"""Vault filesystem scanner.

Walks an Obsidian vault root recursively, yields markdown files whose
relative path does not match any exclude glob. Read-only. The scanner does
NOT parse frontmatter — that's :mod:`plugin.obsidian.parser`'s job, kept
separate so the scanner remains a simple pure-filesystem component.

Default exclude patterns drop the two directory conventions that ship with
every real Obsidian vault — ``.obsidian/`` (config + plugins) and
``Templates/`` (placeholder bodies, not notes).
"""

from __future__ import annotations

import fnmatch
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

# Patterns are matched against vault-relative POSIX paths. ``**`` matches
# any sub-tree; ``fnmatch`` treats it the same as ``*`` (any chars including
# ``/``) which is what we want for "everything under this directory".
DEFAULT_EXCLUDE_PATTERNS: tuple[str, ...] = (
    ".obsidian/**",
    "Templates/**",
)


@dataclass(frozen=True)
class ScannedNote:
    """One markdown note found in the vault.

    ``relative_path`` is POSIX-style relative to the vault root so it works
    as a stable cross-platform ``source_ref`` suffix. ``text`` is the raw
    file contents (UTF-8, errors replaced) — the parser will pull
    frontmatter out of it.
    """

    relative_path: str
    text: str


class VaultScanner:
    """Walk an Obsidian vault directory and yield :class:`ScannedNote`."""

    __slots__ = ("_root", "_exclude_patterns")

    def __init__(
        self,
        root: Path,
        exclude_patterns: tuple[str, ...] | list[str] | None = None,
    ) -> None:
        self._root = Path(root)
        # ``None`` → defaults; ``[]`` → caller explicitly wants no excludes;
        # otherwise the caller's patterns replace the defaults entirely.
        if exclude_patterns is None:
            self._exclude_patterns: tuple[str, ...] = DEFAULT_EXCLUDE_PATTERNS
        else:
            self._exclude_patterns = tuple(exclude_patterns)

    def scan(self) -> Iterator[ScannedNote]:
        """Yield every non-excluded ``*.md`` note under the vault root.

        Raises :class:`FileNotFoundError` when the root is missing and
        :class:`NotADirectoryError` when the root is a file. Hidden /
        OS-dot files are not specifically filtered — they're caught by the
        ``.obsidian/**`` default like the rest of the dot-prefixed config.
        """
        if not self._root.exists():
            raise FileNotFoundError(f"obsidian: vault root not found: {self._root}")
        if not self._root.is_dir():
            raise NotADirectoryError(f"obsidian: vault root is not a directory: {self._root}")

        for path in sorted(self._root.rglob("*.md")):
            if not path.is_file():
                continue
            rel = path.relative_to(self._root).as_posix()
            if self._is_excluded(rel):
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            yield ScannedNote(relative_path=rel, text=text)

    def _is_excluded(self, relative_path: str) -> bool:
        """Match ``relative_path`` against every exclude pattern.

        We additionally check each parent directory so ``.obsidian/**``
        excludes ``.obsidian/plugins/foo/data.md`` correctly under fnmatch
        semantics (which doesn't treat ``**`` differently from ``*``).
        """
        candidates = [relative_path]
        # Build up "first-segment/", "first-segment/second-segment/" probes
        # so an exclude like "Templates/**" matches "Templates/sub/deep.md".
        parts = relative_path.split("/")
        for i in range(1, len(parts)):
            candidates.append("/".join(parts[:i]) + "/" + "anything")
        for pattern in self._exclude_patterns:
            for candidate in candidates:
                if fnmatch.fnmatchcase(candidate, pattern):
                    return True
        return False


__all__ = ["DEFAULT_EXCLUDE_PATTERNS", "ScannedNote", "VaultScanner"]
