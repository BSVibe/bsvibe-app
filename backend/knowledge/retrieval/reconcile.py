"""Embedding reconcile / backfill (Lift 3).

The vector index (``note_embeddings``) is populated event-driven on note writes
(:class:`~backend.knowledge.retrieval.vector_subscriber.VectorSubscriber` +
the settle hook). Two gaps leave knowledge un-retrievable:

* **bulk-imported notes** that predate / bypassed the event path, and
* **concepts**, which fire no write event on creation,

so a corpus can be largely un-embedded (observed: 26 / 1373). This reconcile
enumerates the knowledge layers, diffs against what is already embedded under
the current model, and embeds only the gap — idempotent (a second pass is a
no-op) and model-aware (a model swap re-embeds, via ``existing_paths``).

Only the *knowledge* layers are embedded — ``garden`` (seedlings/entities) and
``concepts`` — never the machinery (``actions`` / ``proposals`` / ``decisions``),
which are the canonicalization action log, not retrievable knowledge.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog

from backend.knowledge.retrieval.vector_subscriber import (
    _DEFAULT_MAX_EMBED_CHARS,
    embed_and_store_note,
)

if TYPE_CHECKING:
    from backend.knowledge.graph.vault import Vault
    from backend.knowledge.retrieval.embedder import Embedder
    from backend.knowledge.retrieval.storage.backend import NoteVectorBackend

logger = structlog.get_logger(__name__)

#: Vault subtrees that hold retrievable knowledge (recursively walked).
KNOWLEDGE_LAYERS: tuple[str, ...] = ("garden", "concepts")


@dataclass(frozen=True)
class ReconcileResult:
    """Outcome of a reconcile pass."""

    scanned: int
    embedded: int
    already: int
    disabled: bool = False


async def reconcile_embeddings(
    vault: Vault,
    embedder: Embedder,
    vector_store: NoteVectorBackend,
    *,
    layers: tuple[str, ...] = KNOWLEDGE_LAYERS,
    max_embed_chars: int = _DEFAULT_MAX_EMBED_CHARS,
) -> ReconcileResult:
    """Embed every knowledge note that lacks a current-model vector. Idempotent."""
    if not embedder.enabled:
        return ReconcileResult(scanned=0, embedded=0, already=0, disabled=True)

    existing = await vector_store.existing_paths()
    scanned = embedded = already = 0
    seen: set[str] = set()

    for layer in layers:
        for abs_path in await vault.read_notes(layer, recursive=True):
            note_path = abs_path.relative_to(vault.root).as_posix()
            if note_path in seen:
                continue
            seen.add(note_path)
            scanned += 1
            if note_path in existing:
                already += 1
                continue
            if await embed_and_store_note(
                vault, embedder, vector_store, note_path, max_embed_chars=max_embed_chars
            ):
                embedded += 1

    logger.info("embedding_reconcile_complete", scanned=scanned, embedded=embedded, already=already)
    return ReconcileResult(scanned=scanned, embedded=embedded, already=already)
