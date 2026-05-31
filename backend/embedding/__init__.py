"""Embedding domain (Bundle 1.5b) — settings, providers, vector storage."""

from backend.embedding.column import EmbeddingVector
from backend.embedding.db import (
    AccountEmbeddingSettingsRow,
    GatewayEmbeddingBase,
    IntentDefinitionRow,
    IntentExampleRow,
)
from backend.embedding.provider import (
    EmbeddingProvider,
    LiteLLMEmbeddingProvider,
    build_provider,
)
from backend.embedding.repository import (
    EmbeddingSettingsRepository,
    IntentDuplicateError,
    IntentRepository,
)
from backend.embedding.serialization import (
    deserialize_embedding,
    serialize_embedding,
)
from backend.embedding.service import EmbeddedExample, EmbeddingService
from backend.embedding.settings import EmbeddingSettings
from backend.embedding.storage import (
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
