"""FileIndexReader — markdown-based index reader/writer for local vault."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import structlog
import yaml

from backend.knowledge.graph.markdown_utils import extract_frontmatter, extract_title
from backend.knowledge.ingest.index_reader import NoteSummary

if TYPE_CHECKING:
    from backend.knowledge.graph.vault import Vault
    from backend.knowledge.retrieval.ontology import OntologyRegistry

logger = structlog.get_logger(__name__)

_INDEX_DIR = "_index"


def _category_to_filename(category: str) -> str:
    """Convert a vault category path to an index filename.

    Examples:
        "garden/idea"    → "garden-ideas.md"
        "garden/insight" → "garden-insights.md"
        "garden/project" → "garden-projects.md"
        "seeds"          → "seeds.md"
    """
    return category.replace("/", "-") + "s.md" if "/" in category else category + ".md"


def _note_to_summary(rel_path: str, content: str) -> NoteSummary:
    """Parse a vault note into a NoteSummary from its frontmatter + title."""
    fm = extract_frontmatter(content)
    title = fm.get("title", "") or extract_title(content) or Path(rel_path).stem

    raw_tags = fm.get("tags", [])
    if isinstance(raw_tags, str):
        tags = [raw_tags]
    elif isinstance(raw_tags, list):
        tags = [str(t).strip() for t in raw_tags if t]
    else:
        tags = []

    raw_related = fm.get("related", [])
    if isinstance(raw_related, str):
        related = [raw_related]
    elif isinstance(raw_related, list):
        related = [str(r).strip() for r in raw_related if r]
    else:
        related = []

    return NoteSummary(
        path=rel_path,
        title=title,
        note_type=fm.get("type", ""),
        tags=tags,
        source=fm.get("source", ""),
        captured_at=fm.get("captured_at", ""),
        related=related,
    )


def _render_index_markdown(category: str, summaries: list[NoteSummary]) -> str:
    """Render an index file as markdown with frontmatter + table."""
    now = datetime.now(tz=UTC).isoformat(timespec="seconds")
    fm = {
        "type": "index",
        "scope": category,
        "updated_at": now,
        "total_notes": len(summaries),
    }
    fm_str = yaml.dump(fm, default_flow_style=False, allow_unicode=True).strip()

    lines = [f"---\n{fm_str}\n---\n"]
    lines.append("| Note | Tags | Source | Date |")
    lines.append("|------|------|--------|------|")

    for s in sorted(summaries, key=lambda x: x.captured_at or "0000-00-00", reverse=True):
        tags_str = ", ".join(f"#{t}" for t in s.tags) if s.tags else ""
        title = s.title.replace("|", "\\|")
        source = (s.source or "").replace("|", "\\|")
        note_link = f"[[{title}]]"
        lines.append(f"| {note_link} | {tags_str} | {source} | {s.captured_at} |")

    lines.append("")
    return "\n".join(lines)


class FileIndexReader:
    """Markdown-file-based index reader/writer for local vaults.

    Stores index files in ``vault/_index/`` as markdown tables.
    Obsidian can display these natively.
    """

    def __init__(self, vault: Vault, ontology: OntologyRegistry | None = None) -> None:
        self._vault = vault
        self._ontology = ontology
        self._entries: dict[str, NoteSummary] = {}  # keyed by note_path
        self._loaded = False

    @property
    def _index_dir(self) -> Path:
        return self._vault.root / _INDEX_DIR

    def _resolve_categories(self) -> list[str]:
        """Categories the index reader scans.

        Walks the maturity tree (``garden/seedling``, ``garden/budding``,
        ``garden/evergreen``) plus the entity stub folder, with seeds at
        the top. Legacy entity-type folders (``ideas/``, ``insights/``...)
        are scanned too — vaults migrated by ``bsage migrate-flatten-vault``
        no longer have them, but unmigrated vaults still need indexing.
        """
        cats: list[str] = [
            "seeds",
            "garden/seedling",
            "garden/budding",
            "garden/evergreen",
            "garden/entities",
        ]
        # Discover any non-system directory at vault root for unmigrated
        # legacy layouts (ideas/, insights/, etc.). Order is preserved so
        # the maturity-based primary scan happens first.
        skip = {"seeds", "actions", "tmp", "node_modules", "garden", ".bsage"}
        for child in sorted(self._vault.root.iterdir()):
            if not child.is_dir() or child.name.startswith((".", "_")):
                continue
            if child.name in skip:
                continue
            cats.append(child.name)
        return cats

    async def _ensure_loaded(self) -> None:
        """Load all index files from disk into memory on first access.

        Marks loaded only when at least one entry was found,
        allowing a retry on next call if the vault was empty or unavailable.
        """
        if self._loaded:
            return
        await self._load_all()
        self._loaded = bool(self._entries)

    async def _load_all(self) -> None:
        """Scan vault notes and populate _entries on first access.

        Reads note files directly rather than parsing index markdown,
        since the table format does not preserve all fields (e.g. path).
        """
        categories = self._resolve_categories()
        for cat in categories:
            try:
                await self._scan_category(cat)
            except (FileNotFoundError, OSError):
                logger.debug("load_scan_failed", category=cat, exc_info=True)
        logger.debug("file_index_loaded", entries=len(self._entries))

    async def _scan_category(self, category: str) -> None:
        """Scan a vault category and populate _entries."""
        try:
            note_paths = await self._vault.read_notes(category)
        except (FileNotFoundError, OSError):
            note_paths = []

        for abs_path in note_paths:
            rel_path = str(abs_path.relative_to(self._vault.root))
            try:
                content = await self._vault.read_note_content(abs_path)
                self._entries[rel_path] = _note_to_summary(rel_path, content)
            except (FileNotFoundError, OSError, UnicodeDecodeError, ValueError, KeyError):
                logger.debug("scan_note_failed", path=rel_path, exc_info=True)

        # Also scan subdirectories (e.g. seeds/telegram-input/)
        base = self._vault.resolve_path(category)

        def _list_subdirs() -> list[str]:
            if not base.is_dir():
                return []
            return [child.name for child in sorted(base.iterdir()) if child.is_dir()]

        subdirs = await asyncio.to_thread(_list_subdirs)
        for name in subdirs:
            child_sub = f"{category}/{name}"
            try:
                paths = await self._vault.read_notes(child_sub)
                for p in paths:
                    rel = str(p.relative_to(self._vault.root))
                    content = await self._vault.read_note_content(p)
                    self._entries[rel] = _note_to_summary(rel, content)
            except (FileNotFoundError, OSError, UnicodeDecodeError, ValueError):
                logger.debug("scan_subdir_failed", subdir=child_sub, exc_info=True)

    async def get_summaries(self, category: str) -> list[NoteSummary]:
        """Return all note summaries for a category."""
        await self._ensure_loaded()
        prefix = category.rstrip("/") + "/"
        return [s for s in self._entries.values() if s.path.startswith(prefix)]

    async def get_all_summaries(self) -> list[NoteSummary]:
        """Return all note summaries across all categories."""
        await self._ensure_loaded()
        return list(self._entries.values())

    async def update_entry(self, note_path: str, summary: NoteSummary) -> None:
        """Add or update a single entry and persist the affected index file."""
        self._entries[note_path] = summary
        category = self._path_to_category(note_path)
        if category:
            await self._write_index_file(category)

    async def remove_entry(self, note_path: str) -> None:
        """Remove an entry and persist the affected index file."""
        if note_path in self._entries:
            category = self._path_to_category(note_path)
            del self._entries[note_path]
            if category:
                await self._write_index_file(category)

    async def rebuild(self, category: str) -> None:
        """Rebuild the index for a category by scanning vault files."""
        # Remove existing entries for this category
        prefix = category.rstrip("/") + "/"
        to_remove = [p for p in self._entries if p.startswith(prefix)]
        for p in to_remove:
            del self._entries[p]

        await self._scan_category(category)

        await self._write_index_file(category)
        count = sum(1 for s in self._entries.values() if s.path.startswith(prefix))
        logger.info("index_rebuilt", category=category, entries=count)

    async def rebuild_all(self) -> None:
        """Rebuild indexes for all standard vault categories."""
        categories = ["seeds", "garden/idea", "garden/insight", "garden/project"]
        for cat in categories:
            try:
                await self.rebuild(cat)
            except (FileNotFoundError, OSError, UnicodeDecodeError, ValueError):
                logger.warning("rebuild_failed", category=cat, exc_info=True)
        await self._write_overview()

    async def _write_index_file(self, category: str) -> None:
        """Persist the index for a category as a markdown file."""
        summaries = await self.get_summaries(category)
        content = _render_index_markdown(category, summaries)
        filename = _category_to_filename(category)

        def _write() -> None:
            idx_dir = self._index_dir
            idx_dir.mkdir(parents=True, exist_ok=True)
            (idx_dir / filename).write_text(content, encoding="utf-8")

        await asyncio.to_thread(_write)
        logger.debug("index_file_written", category=category, filename=filename)

    async def _write_overview(self) -> None:
        """Write a vault overview index file."""
        now = datetime.now(tz=UTC).isoformat(timespec="seconds")
        categories: dict[str, int] = {}
        for s in self._entries.values():
            cat = self._path_to_category(s.path) or "other"
            categories[cat] = categories.get(cat, 0) + 1

        fm = {"type": "index", "scope": "overview", "updated_at": now}
        fm_str = yaml.dump(fm, default_flow_style=False, allow_unicode=True).strip()

        lines = [f"---\n{fm_str}\n---\n"]
        lines.append("# Vault Overview\n")
        lines.append("| Category | Notes |")
        lines.append("|----------|-------|")
        for cat, count in sorted(categories.items()):
            lines.append(f"| {cat} | {count} |")
        lines.append(f"\n**Total**: {len(self._entries)} notes")
        lines.append("")

        content = "\n".join(lines)

        def _write() -> None:
            idx_dir = self._index_dir
            idx_dir.mkdir(parents=True, exist_ok=True)
            (idx_dir / "overview.md").write_text(content, encoding="utf-8")

        await asyncio.to_thread(_write)

    @staticmethod
    def _path_to_category(note_path: str) -> str:
        """Derive the category from a vault-relative path.

        Only ``garden`` uses two-level categories to match ``rebuild_all``.

        "garden/idea/my-note.md"   → "garden/idea"
        "seeds/chat/2026-03-11.md" → "seeds"
        "seeds/2026-03-11.md"      → "seeds"
        """
        parts = Path(note_path).parts
        if len(parts) >= 3 and parts[0] == "garden":
            return f"{parts[0]}/{parts[1]}"
        if len(parts) >= 2:
            return parts[0]
        return ""

    async def index_note_from_content(self, note_path: str, content: str) -> None:
        """Parse a note and update the index — called by IndexSubscriber."""
        summary = _note_to_summary(note_path, content)
        await self.update_entry(note_path, summary)

    async def write_catalog(self) -> None:
        """Generate a human-browsable index.md at the vault root.

        Groups notes by ``note_type`` with wikilinks and tags,
        inspired by Karpathy Wiki's browsable catalog pattern.
        """
        await self._ensure_loaded()
        summaries = list(self._entries.values())

        # Group by note_type
        by_type: dict[str, list[NoteSummary]] = {}
        for s in summaries:
            key = s.note_type or "uncategorized"
            by_type.setdefault(key, []).append(s)

        lines = ["# Knowledge Index\n"]
        lines.append(f"*Auto-generated. {len(summaries)} notes.*\n")

        for type_name in sorted(by_type):
            group = by_type[type_name]
            lines.append(f"## {type_name.title()} ({len(group)})\n")
            for s in sorted(group, key=lambda x: x.captured_at or "0000", reverse=True):
                tags = " ".join(f"#{t}" for t in s.tags[:3]) if s.tags else ""
                entry = f"- [[{s.title}]]"
                if tags:
                    entry += f" {tags}"
                lines.append(entry)
            lines.append("")

        content = "\n".join(lines)
        catalog_path = self._vault.root / "index.md"
        await asyncio.to_thread(catalog_path.write_text, content, encoding="utf-8")
        logger.info("catalog_written", notes=len(summaries))
