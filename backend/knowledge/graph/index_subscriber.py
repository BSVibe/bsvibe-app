"""IndexSubscriber — EventBus subscriber that updates vault index after writes."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from backend.knowledge._internal.events import Event
    from backend.knowledge.graph.vault import Vault
    from backend.knowledge.ingest.index_reader import IndexReader

logger = structlog.get_logger(__name__)


class IndexSubscriber:
    """Listens for vault write events and updates the markdown index files.

    Follows the same ``EventSubscriber`` protocol as
    ``WebSocketEventBroadcaster``.
    """

    def __init__(self, index_reader: IndexReader, vault: Vault) -> None:
        self._index_reader = index_reader
        self._vault = vault

    async def on_event(self, event: Event) -> None:
        """Handle an event from the EventBus.

        Handles SEED_WRITTEN, GARDEN_WRITTEN, NOTE_UPDATED (index/re-index)
        and NOTE_DELETED (remove from index).
        """
        from backend.knowledge._internal.events import EventType

        if event.event_type == EventType.NOTE_DELETED:
            path_str = event.payload.get("path", "")
            if not path_str:
                return
            try:
                abs_path = Path(path_str)
                rel_path = str(abs_path.relative_to(self._vault.root))
                await self._index_reader.remove_entry(rel_path)
            except Exception:
                logger.warning("index_on_delete_failed", path=path_str, exc_info=True)
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
            await self._index_reader.index_note_from_content(rel_path, content)
        except Exception:
            logger.warning("index_on_write_failed", path=path_str, exc_info=True)
