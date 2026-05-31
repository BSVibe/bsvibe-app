"""SemanticNoteRetriever — embed the signals, search the note vector store (G5).

Closes the loop the proposal's §5.4 "의미 기반 note 검색" describes: it embeds the
change's signals and runs a pgvector similarity search over the workspace's note
embeddings, surfacing the most relevant notes as statements folded into the
SAME composite the verifier (B3) + work-start seed (B6) consume.

Satisfies the :class:`~backend.workflow.application.verification_service.CanonRetriever`
Protocol (``retrieve_for_signals(signals) -> list[str]``). Discipline matches the
other sources: graceful-empty (embedding disabled / no signals / no hits → ``[]``)
and never-raises into the verify path. Auxiliary by design (proposal §5.4): a
similarity floor keeps weak matches out, and the result is capped.
"""

from __future__ import annotations

import structlog

from backend.knowledge.retrieval.embedder import Embedder
from backend.knowledge.retrieval.storage.backend import NoteVectorBackend

logger = structlog.get_logger(__name__)

#: Conservative cap — semantic search is the auxiliary source, so it contributes
#: a few related notes, not a flood (the composite re-caps the merged list too).
_DEFAULT_TOP_K = 3

#: Cosine-similarity floor. Below this a "match" is noise; dropping it keeps an
#: unrelated note out of the verify contract / seed context.
_DEFAULT_MIN_SIMILARITY = 0.5


class SemanticNoteRetriever:
    """Embed signals → pgvector note search → related-note statements."""

    def __init__(
        self,
        embedder: Embedder,
        backend: NoteVectorBackend,
        *,
        top_k: int = _DEFAULT_TOP_K,
        min_similarity: float = _DEFAULT_MIN_SIMILARITY,
    ) -> None:
        self._embedder = embedder
        self._backend = backend
        self._top_k = top_k
        self._min_similarity = min_similarity

    async def retrieve_for_signals(self, signals: str) -> list[str]:
        try:
            return await self._retrieve(signals)
        except Exception:  # noqa: BLE001 — verify path must never crash on search
            logger.warning("semantic_note_retrieve_failed", exc_info=True)
            return []

    async def _retrieve(self, signals: str) -> list[str]:
        if not self._embedder.enabled or not signals.strip():
            return []
        query = await self._embedder.embed(signals)
        if not query:
            return []
        hits = await self._backend.search(query, top_k=self._top_k)
        return [f"Related note — {path}" for path, score in hits if score >= self._min_similarity]


__all__ = ["SemanticNoteRetriever"]
