"""NoteRepository Protocol — read/write seam for garden notes.

v8 D44/D45. Knowledge's source of truth lives on the filesystem (the Vault).
The Protocol here abstracts away the
:class:`~backend.knowledge.graph.storage.StorageBackend` /
:class:`~backend.knowledge.graph.writer_core.GardenWriter` mechanics so
Knowledge application code (ingest, retrieval, canonicalization) reads +
writes notes through a stable seam.

Method surface intentionally minimal — only the patterns existing callers
actually need today. Adding a real caller justifies adding a method; never
speculatively.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class NoteRecord:
    """A vault note exposed across the repository seam.

    The ``path`` is the vault-relative POSIX path (the natural primary key for
    a filesystem-backed note). The ``content`` is the raw markdown body
    including frontmatter — interpretation (frontmatter extraction, etc.) is
    the caller's concern, since different application sites want different
    slices and forcing the repository to materialize a single rich shape
    would couple it to the canonicalization model layer.
    """

    path: str
    content: str


@runtime_checkable
class NoteRepository(Protocol):
    """Persistence seam for garden notes (vault-backed)."""

    async def read(self, path: str) -> str:
        """Return the raw markdown content at this vault-relative path.

        Raises :class:`FileNotFoundError` (or backend equivalent) when the
        path does not exist — the Protocol does not invent a new error
        hierarchy; callers handle backend exceptions where they handle them
        today.
        """

    async def exists(self, path: str) -> bool:
        """Return ``True`` iff a note exists at this vault-relative path."""

    async def list_paths(self, subdir: str, *, pattern: str = "*.md") -> list[str]:
        """List vault-relative paths under ``subdir`` matching ``pattern``.

        ``subdir`` is treated as vault-relative; the repository never escapes
        the workspace boundary the underlying storage was constructed with.
        """

    async def write(self, path: str, content: str) -> None:
        """Write ``content`` to the vault at ``path``.

        Creates parent directories on demand. The repository does NOT manage
        frontmatter shape or slugging — callers ship the already-rendered
        markdown body. (Higher-level write surfaces — e.g. ``write_garden``
        on the existing :class:`~backend.knowledge.graph.writer_core.GardenWriter`
        — still live where they are; this Protocol carries the *byte-level*
        seam so non-GardenNote writes have a stable abstraction.)
        """

    async def delete(self, path: str) -> None:
        """Delete the note at ``path``. Missing files are tolerated (idempotent)."""


__all__ = ["NoteRecord", "NoteRepository"]
