"""Embedding domain (Bundle 1.5b) — settings, providers, vector storage."""

from backend.router.embedding.column import EmbeddingVector
from backend.router.embedding.db import (
    AccountEmbeddingSettingsRow,
    GatewayEmbeddingBase,
    IntentDefinitionRow,
    IntentExampleRow,
)
from backend.router.embedding.provider import (
    EmbeddingProvider,
    LiteLLMEmbeddingProvider,
    build_provider,
)
from backend.router.embedding.repository import (
    EmbeddingSettingsRepository,
    IntentDuplicateError,
    IntentRepository,
)
from backend.router.embedding.serialization import (
    deserialize_embedding,
    serialize_embedding,
)
from backend.router.embedding.service import EmbeddedExample, EmbeddingService
from backend.router.embedding.settings import EmbeddingSettings
from backend.router.embedding.storage import (
    InMemoryVectorBackend,
    SearchHit,
    VectorEntry,
    VectorSearchBackend,
)

__all__ = [
    "AccountEmbeddingSettingsRow",
    "EmbeddedExample",
    "EmbeddingProvider",
    "EmbeddingSettings",
    "EmbeddingSettingsRepository",
    "EmbeddingService",
    "EmbeddingVector",
    "GatewayEmbeddingBase",
    "InMemoryVectorBackend",
    "IntentDefinitionRow",
    "IntentDuplicateError",
    "IntentExampleRow",
    "IntentRepository",
    "LiteLLMEmbeddingProvider",
    "SearchHit",
    "VectorEntry",
    "VectorSearchBackend",
    "build_provider",
    "deserialize_embedding",
    "serialize_embedding",
]
