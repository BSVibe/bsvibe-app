"""Vector-search backends — pgvector for prod, in-memory for tests."""

from backend.router.embedding.storage.backend import (
    SearchHit,
    VectorEntry,
    VectorSearchBackend,
)
from backend.router.embedding.storage.memory import InMemoryVectorBackend

__all__ = [
    "InMemoryVectorBackend",
    "SearchHit",
    "VectorEntry",
    "VectorSearchBackend",
]
