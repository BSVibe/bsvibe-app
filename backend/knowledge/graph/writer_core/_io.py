"""_WriterIOMixin — primitive ingest writes (seeds, garden notes, logs, reads).

Extracted from the original monolithic ``writer_core.py`` during Lift L1
(v8 §17.3). Behaviour is identical to the pre-decomp implementation; only
the file boundary changed.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog
import yaml

from backend.knowledge._internal.events import emit_event
from backend.knowledge.graph.note import (
    _MAX_ACTION_SUMMARY,
    GardenNote,
    build_frontmatter,
    slugify,
)
from backend.knowledge.graph.vault import Vault
from backend.knowledge.graph.writer_core._entity_stub import (
    _create_entity_stub,
    _update_entity_stub_mentions,
)

if TYPE_CHECKING:
    from backend.knowledge._internal.events import EventBus

    # TODO(bundle-k-integration): wire to plugin.audit -- original: from bsage.garden.audit_outbox import AiosqliteAuditOutbox
    AiosqliteAuditOutbox = Any
    from backend.knowledge.graph.sync import SyncManager
    from backend.knowledge.retrieval.ontology import OntologyRegistry

logger = structlog.get_logger(__name__)


class _WriterIOMixin:
    """Primitive ingest writes: seeds, garden notes, and append-only logs.

    Implementations rely on the attributes (``_vault``, ``_seed_lock``,
    ``_garden_lock``, ``_log_lock``, ``_event_bus``, ``_ontology``) initialised
    by :class:`GardenWriter`.
    """

    # --- attribute declarations for type checkers --------------------------
    _vault: Vault
    _sync_manager: SyncManager | None
    _event_bus: EventBus | None
    _ontology: OntologyRegistry | None
    _audit_outbox: AiosqliteAuditOutbox | None
    _default_tenant_id: str | None
    _log_lock: asyncio.Lock
    _garden_lock: asyncio.Lock
    _seed_lock: asyncio.Lock

    # --- helpers expected to be provided by GardenWriter -------------------
    async def _notify_sync(
        self, event_type_str: str, path: Path, source: str
    ) -> None:  # pragma: no cover - implemented in GardenWriter
        ...

    async def _emit_vault_modified(
        self,
        *,
        path: Path,
        operation: str,
        source: str,
        note_type: str | None = None,
        tenant_id: str | None = None,
    ) -> None:  # pragma: no cover - implemented in GardenWriter
        ...

    @staticmethod
    def _find_dedup_path(
        directory: Path, slug: str
    ) -> Path:  # pragma: no cover - implemented in GardenWriter
        raise NotImplementedError

    def _resolve_folder(self, note: GardenNote | None = None) -> str:
        """Resolve the vault folder for a note from its maturity.

        Andy Matuschak-style three-stage layout: ``garden/seedling``
        (just captured), ``garden/budding`` (in progress), and
        ``garden/evergreen`` (curated). Identity comes from connections,
        not from note kind, so the folder reflects where in the growth
        cycle the note sits — not what it's about.

        ``None`` defaults to ``seedling`` for the bulk-import path. Any
        unrecognised maturity string falls back to ``seedling`` so a
        typo in frontmatter doesn't strand a note in some
        ``garden/banana/`` folder.
        """
        valid = {"seedling", "budding", "evergreen"}
        maturity = (note.maturity if note else None) or "seedling"
        if maturity not in valid:
            maturity = "seedling"
        return f"garden/{maturity}"

    def resolve_plugin_state_path(self, plugin_name: str, subpath: str = "_state.json") -> Path:
        """Resolve a plugin state file path within the vault.

        Plugins use this to safely store persistent state (e.g., polling cursors, offsets)
        without accessing private vault APIs.
        """
        return self._vault.resolve_path(f"seeds/{plugin_name}/{subpath}")

    async def write_seed(self, source: str, data: dict[str, Any]) -> Path:
        """Write raw collected data as a seed note.

        Creates a file at seeds/{source}/{YYYY-MM-DD_HHMM}.md with
        YAML frontmatter containing type, source, and captured_at.
        """
        now = datetime.now(tz=UTC)
        date_str = now.strftime("%Y-%m-%d")

        source_dir = self._vault.resolve_path(f"seeds/{source}")
        source_dir.mkdir(parents=True, exist_ok=True)

        metadata: dict[str, Any] = {
            "type": "seed",
            "source": source,
            "captured_at": date_str,
        }
        if "title" in data:
            metadata["title"] = data["title"]
        if "tags" in data:
            metadata["tags"] = data["tags"]

        frontmatter = build_frontmatter(metadata)

        if "title" in data and "content" in data:
            body = data["content"]
        else:
            body = yaml.dump(data, default_flow_style=False, allow_unicode=True)
        content = f"{frontmatter}\n{body}\n"

        async with self._seed_lock:
            filename = now.strftime("%Y-%m-%d_%H%M%S") + ".md"
            file_path = source_dir / filename
            if file_path.exists():
                slug = now.strftime("%Y-%m-%d_%H%M%S")
                file_path = self._find_dedup_path(source_dir, slug)
            await asyncio.to_thread(file_path.write_text, content, encoding="utf-8")

        logger.info("seed_written", source=source, path=str(file_path))
        await self._notify_sync("seed", file_path, source)
        await emit_event(
            self._event_bus, "SEED_WRITTEN", {"path": str(file_path), "source": source}
        )
        await self._emit_vault_modified(
            path=file_path,
            operation="seed_written",
            source=source,
            note_type="seed",
        )
        return file_path

    async def write_garden(self, note: GardenNote | dict[str, Any]) -> Path:
        """Write a processed garden note with deduplication.

        v2.2: Uses the ontology folder mapping (e.g. ``ideas/``, ``events/``)
        instead of ``garden/{type}/``. Falls back to ``{note_type}/`` if no
        mapping exists.
        """
        if isinstance(note, dict):
            note = GardenNote(**note)

        async with self._garden_lock:
            now = datetime.now(tz=UTC)
            date_str = now.strftime("%Y-%m-%d")
            slug = slugify(note.title)

            # Maturity-based layout: garden/seedling, /budding, /evergreen.
            folder = self._resolve_folder(note)
            type_dir = self._vault.resolve_path(folder)
            type_dir.mkdir(parents=True, exist_ok=True)

            file_path = type_dir / f"{slug}.md"
            if file_path.exists():
                file_path = self._find_dedup_path(type_dir, slug)

            related_links = [f"[[{r}]]" for r in note.related]

            metadata: dict[str, Any] = {
                "status": "seed",
                "source": note.source,
                "maturity": note.maturity,
                "captured_at": date_str,
                "confidence": note.confidence,
                "knowledge_layer": note.knowledge_layer,
            }
            # Legacy ``type:`` field — kept only when caller explicitly set it.
            if note.note_type:
                metadata["type"] = note.note_type
            # Phase 0 P0.5 — tenant isolation.
            tenant_id = note.tenant_id or self._default_tenant_id
            if tenant_id:
                metadata["tenant_id"] = tenant_id
            if note.aliases:
                metadata["aliases"] = note.aliases
            for key, value in note.extra_fields.items():
                metadata[key] = value
            for rel_type, targets in note.relations.items():
                metadata[rel_type] = targets
            if related_links:
                metadata["related"] = related_links
            if note.tags:
                metadata["tags"] = note.tags
            if note.entities:
                metadata["entities"] = note.entities

            frontmatter = build_frontmatter(metadata)
            content = f"{frontmatter}\n# {note.title}\n\n{note.content}\n"

            await asyncio.to_thread(file_path.write_text, content, encoding="utf-8")

        logger.info(
            "garden_note_written",
            title=note.title,
            note_type=note.note_type,
            path=str(file_path),
        )
        await self._notify_sync("garden", file_path, note.source)
        await emit_event(
            self._event_bus, "GARDEN_WRITTEN", {"path": str(file_path), "source": note.source}
        )
        await self._emit_vault_modified(
            path=file_path,
            operation="garden_written",
            source=note.source,
            note_type=note.note_type,
            tenant_id=tenant_id,
        )
        return file_path

    async def ensure_entity_stub(self, name: str, mentioned_in: Path) -> Path:
        """Auto-create or update a ``garden/entities/<slug>.md`` stub for ``[[name]]``."""
        clean = name.strip()
        if not clean:
            raise ValueError("ensure_entity_stub requires a non-empty name")
        slug = slugify(clean)

        async with self._garden_lock:
            entities_dir = self._vault.resolve_path("garden/entities")
            entities_dir.mkdir(parents=True, exist_ok=True)
            file_path = entities_dir / f"{slug}.md"

            try:
                rel_mention = mentioned_in.relative_to(self._vault.root)
                rel_str = str(rel_mention)
            except ValueError:
                rel_str = str(mentioned_in)

            if file_path.exists():
                _update_entity_stub_mentions(file_path, rel_str)
            else:
                _create_entity_stub(file_path, clean, rel_str)

        logger.debug("entity_stub_ensured", name=clean, path=str(file_path))
        return file_path

    async def write_action(self, skill_name: str, summary: str) -> None:
        """Append an action log entry to the daily action log."""
        now = datetime.now(tz=UTC)
        date_str = now.strftime("%Y-%m-%d")
        time_str = now.strftime("%H:%M:%S")

        actions_dir = self._vault.resolve_path("actions")
        actions_dir.mkdir(parents=True, exist_ok=True)

        if len(summary) > _MAX_ACTION_SUMMARY:
            truncated = summary[:_MAX_ACTION_SUMMARY] + "…"
        else:
            truncated = summary
        log_path = actions_dir / f"{date_str}.md"
        entry = f"- **{time_str}** | `{skill_name}` | {truncated}\n"

        def _write() -> None:
            if log_path.exists():
                with log_path.open("a", encoding="utf-8") as f:
                    f.write(entry)
            else:
                log_path.write_text(f"# Actions — {date_str}\n\n" + entry, encoding="utf-8")

        async with self._log_lock:
            await asyncio.to_thread(_write)
        logger.info("action_logged", skill_name=skill_name, path=str(log_path))
        await self._notify_sync("action", log_path, skill_name)
        await emit_event(
            self._event_bus, "ACTION_LOGGED", {"path": str(log_path), "source": skill_name}
        )

    async def write_input_log(self, source: str, raw_text: str) -> None:
        """Write raw input data to the input-log directory for transparency."""
        now = datetime.now(tz=UTC)
        date_str = now.strftime("%Y-%m-%d")
        time_str = now.strftime("%H:%M:%S")

        log_dir = self._vault.resolve_path("actions/input-log")
        await asyncio.to_thread(log_dir.mkdir, parents=True, exist_ok=True)

        log_path = log_dir / f"{date_str}.md"
        truncated = raw_text[:500] if len(raw_text) > 500 else raw_text
        entry = f"- **{time_str}** | `{source}` | {truncated}\n"

        def _write() -> None:
            if log_path.exists():
                with log_path.open("a", encoding="utf-8") as f:
                    f.write(entry)
            else:
                log_path.write_text(f"# Input Log — {date_str}\n\n" + entry, encoding="utf-8")

        async with self._log_lock:
            await asyncio.to_thread(_write)
        logger.debug("input_log_written", source=source, path=str(log_path))

    async def read_notes(self, subdir: str) -> list[Path]:
        """Read notes from a vault subdirectory. Delegates to the vault."""
        return await self._vault.read_notes(subdir)

    async def read_note_content(self, path: Path) -> str:
        """Read the text content of a note file asynchronously. Delegates to the vault."""
        return await self._vault.read_note_content(path)
