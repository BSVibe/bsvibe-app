"""Per-chunk related-context retrieval for :mod:`ingest_compiler`.

Lift L3 (v8 §17.6) carves out the retriever-facing concern so the
invariant is impossible to miss in a future refactor:

    ⚠️  THIS HELPER IS CALLED PER CHUNK, INSIDE THE CHUNK LOOP.

The ``rag-batch-stale-related-context`` skill captures the exact bug
that motivated this carve-out: a previous version computed related
context ONCE outside the chunk loop and reused it across every chunk,
which silently broke the *update* path — chunks #2+ saw context
irrelevant to their own seeds and so every action looked like a
``create``. Tests like ``TestPerChunkRelatedContextInvariant`` lock
the call count to ``len(chunks)`` to prevent regression.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backend.knowledge.retrieval.retriever import VaultRetriever


async def find_related(
    retriever: VaultRetriever | None,
    seed_content: str,
) -> str:
    """Search vault for notes related to seed content.

    ``seed_content`` is built from the CURRENT chunk's seeds — typically a
    join of the first ~500 chars of each :class:`BatchItem` in the chunk.
    DO NOT cache or hoist this call outside the per-chunk loop; the
    relevance budget is per-chunk by design.
    """
    if retriever is None:
        return "No existing notes available."
    return await retriever.search(query=seed_content)
