"""Note vector-search storage backends (G3).

``NoteVectorBackend`` Protocol + a pgvector prod impl and an in-memory test
impl, mirroring :mod:`backend.router.embedding.storage`.
"""

from __future__ import annotations

from backend.knowledge.retrieval.storage.backend import (
    NoteVectorBackend,
    cosine_similarity,
)
from backend.knowledge.retrieval.storage.memory import InMemoryNoteVectorBackend
from backend.knowledge.retrieval.storage.pg import PgNoteVectorBackend

__all__ = [
    "InMemoryNoteVectorBackend",
    "NoteVectorBackend",
    "PgNoteVectorBackend",
    "cosine_similarity",
]
