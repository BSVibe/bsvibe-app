"""Vector-search backends — pgvector for prod, in-memory for tests."""

from backend.embedding.storage.backend import (
    SearchHit,
    VectorEntry,
    VectorSearchBackend,
)
from backend.embedding.storage.memory import InMemoryVectorBackend

__all__ = [
    "InMemoryVectorBackend",
    "SearchHit",
    "VectorEntry",
    "VectorSearchBackend",
]
