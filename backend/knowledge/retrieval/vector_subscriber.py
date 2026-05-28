"""VectorSubscriber — computes and stores embeddings on vault write events."""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from backend.knowledge._internal.events import Event, EventType
from backend.knowledge.graph.markdown_utils import body_after_frontmatter, extract_frontmatter

if TYPE_CHECKING:
    from backend.knowledge.graph.vault import Vault
    from backend.knowledge.retrieval.embedder import Embedder
    from backend.knowledge.retrieval.storage.backend import NoteVectorBackend

logger = structlog.get_logger(__name__)

_DEFAULT_MAX_EMBED_CHARS = 8000


class VectorSubscriber:
    """Listens for vault events and updates the vector store.

    Computes embeddings from note title + body on every write event.
    Removes embeddings on delete.
    """

    def __init__(
        self,
        vector_store: NoteVectorBackend,
        vault: Vault,
        embedder: Embedder,
        *,
        max_embed_chars: int = _DEFAULT_MAX_EMBED_CHARS,
    ) -> None:
        self._vector_store = vector_store
        self._vault = vault
        self._embedder = embedder
        self._max_embed_chars = max_embed_chars

    async def on_event(self, event: Event) -> None:
        """Handle an event from the EventBus."""
        if not self._embedder.enabled:
            return

        if event.event_type == EventType.NOTE_DELETED:
            note_path = event.payload.get("path", "")
            if note_path:
                await self._vector_store.remove(note_path)
                logger.debug("vector_removed", path=note_path)
            return

        if event.event_type not in (
            EventType.SEED_WRITTEN,
            EventType.GARDEN_WRITTEN,
            EventType.NOTE_UPDATED,
        ):
            return

        note_path = event.payload.get("path", "")
        if not note_path:
            return

        try:
            abs_path = self._vault.resolve_path(note_path)
            content = await self._vault.read_note_content(abs_path)
        except (FileNotFoundError, OSError, UnicodeDecodeError):
            logger.debug("vector_read_failed", path=note_path)
            return

        fm = extract_frontmatter(content)
        title = fm.get("title", "")
        body = body_after_frontmatter(content)

        text = f"{title}\n{body}".strip()
        if not text:
            return

        if len(text) > self._max_embed_chars:
            logger.warning(
                "vector_text_truncated",
                path=note_path,
                original_len=len(text),
                max_len=self._max_embed_chars,
            )
            text = text[: self._max_embed_chars]

        try:
            embedding = await self._embedder.embed(text)
            await self._vector_store.store(note_path, embedding)
            logger.debug("vector_stored", path=note_path, dim=len(embedding))
        except (RuntimeError, OSError, ValueError):
            logger.warning("vector_embed_failed", path=note_path, exc_info=True)
