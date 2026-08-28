"""Embedding reconcile / backfill (Lift 3).

The vector index (``note_embeddings``) is populated event-driven on note writes
(:func:`~backend.knowledge.retrieval.vector_subscriber.embed_and_store_note` +
the settle hook). Two gaps leave knowledge un-retrievable:

* **bulk-imported notes** that predate / bypassed the event path, and
* **concepts**, which fire no write event on creation,

so a corpus can be largely un-embedded (observed: 26 / 1373). This reconcile
enumerates the knowledge layers, diffs against what is already embedded under
the current model, and embeds only the gap — idempotent (a second pass is a
no-op), model-aware (a model swap re-embeds) and CONTENT-aware (a vector
built from different text re-embeds), via ``existing_fingerprints``.

Only the *knowledge* layers are embedded — ``garden`` (seedlings/entities) and
``concepts`` — never the machinery (``actions`` / ``proposals`` / ``decisions``),
which are the canonicalization action log, not retrievable knowledge.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
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


#: Notes embedded between durable checkpoints. Small enough that a cut
#: connection loses seconds of work, large enough not to commit per note.
_DEFAULT_CHECKPOINT_EVERY = 50


@dataclass(frozen=True)
class ReconcileResult:
    """Outcome of ONE reconcile pass.

    ``scanned`` / ``embedded`` / ``already`` describe the notes this pass
    examined. ``remaining`` is the tail it did NOT examine because it hit
    ``max_embeds`` — call again until it is 0. That IS the resume protocol:
    the pass is fingerprint-keyed, so a second call re-walks the finished
    notes cheaply (a file read + a hash, no embedding request) and picks up
    where the last one stopped. No job row, no cursor, no lease.
    """

    scanned: int
    embedded: int
    already: int
    disabled: bool = False
    remaining: int = 0


async def reconcile_embeddings(
    vault: Vault,
    embedder: Embedder,
    vector_store: NoteVectorBackend,
    *,
    layers: tuple[str, ...] = KNOWLEDGE_LAYERS,
    max_embed_chars: int = _DEFAULT_MAX_EMBED_CHARS,
    max_embeds: int | None = None,
    checkpoint: Callable[[], Awaitable[None]] | None = None,
    checkpoint_every: int = _DEFAULT_CHECKPOINT_EVERY,
) -> ReconcileResult:
    """Embed knowledge notes that lack a current-model vector. Idempotent.

    ``max_embeds`` bounds ONE pass so an HTTP-shaped caller answers before its
    proxy gives up (measured: a 1,685-note pass ran ~12 min and Cloudflare cut
    the client at 125s, so every trigger reported failure for work that
    succeeded). ``checkpoint`` is called every ``checkpoint_every`` embeds —
    the caller commits there, so a cut connection costs seconds, not the pass.
    """
    if not embedder.enabled:
        return ReconcileResult(scanned=0, embedded=0, already=0, disabled=True)

    existing = await vector_store.existing_fingerprints()
    scanned = embedded = already = remaining = 0
    seen: set[str] = set()
    capped = False

    for layer in layers:
        for abs_path in await vault.read_notes(layer, recursive=True):
            note_path = abs_path.relative_to(vault.root).as_posix()
            if note_path in seen:
                continue
            seen.add(note_path)
            # Past the cap we stop EXAMINING rather than guess which of the
            # rest are stale — judging that here would be a second definition
            # of "what text represents this note" (the #837 drift). The tail is
            # reported as un-examined, and the next pass walks it.
            if capped:
                remaining += 1
                continue
            scanned += 1
            # The skip decision is NOT made here. ``embed_and_store_note`` is the
            # only place that knows the embedded text, so it compares the stored
            # fingerprint itself and returns False when nothing changed. Deciding
            # here would need a second copy of "what text represents this note" —
            # the drift that keyed 1,724 prod vectors to a work-log line (#837).
            if await embed_and_store_note(
                vault,
                embedder,
                vector_store,
                note_path,
                max_embed_chars=max_embed_chars,
                known_fingerprint=existing.get(note_path),
            ):
                embedded += 1
                if checkpoint is not None and embedded % checkpoint_every == 0:
                    await checkpoint()
                if max_embeds is not None and embedded >= max_embeds:
                    capped = True
            elif note_path in existing:
                already += 1

    logger.info(
        "embedding_reconcile_complete",
        scanned=scanned,
        embedded=embedded,
        already=already,
        remaining=remaining,
    )
    return ReconcileResult(scanned=scanned, embedded=embedded, already=already, remaining=remaining)
