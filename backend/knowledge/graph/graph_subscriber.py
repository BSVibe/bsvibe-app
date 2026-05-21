"""GraphSubscriber — EventBus subscriber that updates the knowledge graph after vault writes."""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from backend.knowledge._internal.events import Event
    from backend.knowledge.graph.graph_extractor import GraphExtractor
    from backend.knowledge.graph.graph_store import GraphStore
    from backend.knowledge.graph.vault import Vault

logger = structlog.get_logger(__name__)


class GraphSubscriber:
    """Listens for vault write events and updates the knowledge graph.

    Follows the same ``EventSubscriber`` protocol as ``IndexSubscriber``.
    """

    def __init__(self, graph_store: GraphStore, vault: Vault, extractor: GraphExtractor) -> None:
        self._store = graph_store
        self._vault = vault
        self._extractor = extractor

    async def on_event(self, event: Event) -> None:
        """Handle an event from the EventBus."""
        from backend.knowledge._internal.events import EventType

        if event.event_type == EventType.NOTE_DELETED:
            path_str = event.payload.get("path", "")
            if not path_str:
                return
            try:
                abs_path = Path(path_str)
                rel_path = str(abs_path.relative_to(self._vault.root))
                deleted = await self._store.delete_by_source(rel_path)
                logger.info("graph_note_deleted", path=rel_path, removed=deleted)
            except (ValueError, FileNotFoundError, OSError):
                logger.warning("graph_on_delete_failed", path=path_str, exc_info=True)
            return

        if event.event_type not in (
            EventType.SEED_WRITTEN,
            EventType.GARDEN_WRITTEN,
            EventType.NOTE_UPDATED,
        ):
            return

        path_str = event.payload.get("path", "")
        if not path_str:
            return

        abs_path = Path(path_str)
        try:
            rel_path = str(abs_path.relative_to(self._vault.root))
            content = await self._vault.read_note_content(abs_path)

            # Source of Truth: re-extract from markdown (the canonical source).
            # On NOTE_UPDATED, also reset the decay clock for affected entities.
            await self._store.delete_by_source(rel_path)

            entities, relationships = self._extractor.extract_from_note(rel_path, content)

            # Upsert entities and collect resolved IDs
            id_map: dict[str, str] = {}
            for entity in entities:
                resolved_id = await self._store.upsert_entity(entity)
                id_map[entity.id] = resolved_id

            # Upsert relationships with resolved entity IDs
            for rel in relationships:
                resolved = dataclasses.replace(
                    rel,
                    source_id=id_map.get(rel.source_id, rel.source_id),
                    target_id=id_map.get(rel.target_id, rel.target_id),
                )
                await self._store.upsert_relationship(resolved)

            await self._store.commit()

            # Confirm entities to reset decay clock (Source of Truth policy)
            if event.event_type == EventType.NOTE_UPDATED:
                await self._store.confirm_entities_by_source(rel_path)

            # Conflict detection for fact notes
            self._check_fact_conflicts(rel_path, content, entities)

            logger.info(
                "graph_note_indexed",
                path=rel_path,
                entities=len(entities),
                relationships=len(relationships),
            )
        except (ValueError, FileNotFoundError, OSError, UnicodeDecodeError):
            logger.warning("graph_on_write_failed", path=path_str, exc_info=True)

    @staticmethod
    def _check_fact_conflicts(rel_path: str, content: str, entities: list) -> None:
        """Log potential conflicts when a fact note is indexed."""
        from backend.knowledge.graph.markdown_utils import extract_frontmatter

        fm = extract_frontmatter(content)
        if fm.get("type") != "fact":
            return

        subject = fm.get("subject", "")
        predicate = fm.get("predicate", "")
        if not subject or not predicate:
            return

        logger.info(
            "fact_indexed",
            path=rel_path,
            subject=subject,
            predicate=predicate,
            object=fm.get("object", ""),
            source_type=fm.get("source_type", "inferred"),
        )
