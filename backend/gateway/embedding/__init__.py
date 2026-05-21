"""Embedding domain (Bundle 1.5b) — settings, providers, vector storage."""

from backend.gateway.embedding.column import EmbeddingVector
from backend.gateway.embedding.db import (
    AccountEmbeddingSettingsRow,
    GatewayEmbeddingBase,
    IntentDefinitionRow,
    IntentExampleRow,
)
from backend.gateway.embedding.provider import (
    EmbeddingProvider,
    LiteLLMEmbeddingProvider,
    build_provider,
)
from backend.gateway.embedding.repository import (
    EmbeddingSettingsRepository,
    IntentDuplicateError,
    IntentRepository,
)
from backend.gateway.embedding.serialization import (
    deserialize_embedding,
    serialize_embedding,
)
from backend.gateway.embedding.service import EmbeddedExample, EmbeddingService
from backend.gateway.embedding.settings import EmbeddingSettings
from backend.gateway.embedding.storage import (
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
