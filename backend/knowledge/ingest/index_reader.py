"""IndexReader — Protocol and data model for vault note index access."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass
class NoteSummary:
    """Summary of a vault note for index lookup.

    Extracted from frontmatter + filename — no LLM call needed.
    """

    path: str  # vault-relative, e.g. "garden/idea/bsage.md"
    title: str
    note_type: str  # seed / idea / insight / project
    tags: list[str] = field(default_factory=list)
    source: str = ""
    captured_at: str = ""
    related: list[str] = field(default_factory=list)


@runtime_checkable
class IndexReader(Protocol):
    """Abstract interface for reading/writing the vault note index.

    Local implementation uses ``_index/*.md`` markdown files.
    Cloud implementation can swap to a DB backend with the same Protocol.
    """

    async def get_summaries(self, category: str) -> list[NoteSummary]:
        """Return all note summaries for a vault category.

        Args:
            category: Vault subdirectory, e.g. "garden/idea" or "seeds".
        """
        ...

    async def get_all_summaries(self) -> list[NoteSummary]:
        """Return all note summaries across all categories."""
        ...

    async def update_entry(self, note_path: str, summary: NoteSummary) -> None:
        """Add or update a single entry in the index.

        Args:
            note_path: Vault-relative path.
            summary: The note summary to store.
        """
        ...

    async def remove_entry(self, note_path: str) -> None:
        """Remove a note entry from the index.

        Args:
            note_path: Vault-relative path.
        """
        ...

    async def rebuild(self, category: str) -> None:
        """Rebuild the index file for a category from vault files.

        Args:
            category: Vault subdirectory to rebuild.
        """
        ...

    async def index_note_from_content(self, note_path: str, content: str) -> None:
        """Index a note directly from its content (used by event subscribers).

        Args:
            note_path: Vault-relative path.
            content: Full note content including frontmatter.
        """
        ...
