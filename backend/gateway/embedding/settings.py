"""Per-account embedding configuration.

Stored as JSONB on ``account_embedding_settings.config``. Each account
picks its own embedding model — different accounts can use different
providers (Ollama, OpenAI, Cohere). The chosen model is recorded on
every ``intent_examples.embedding_model`` row so we can detect stale
embeddings after a model swap.

No default model: accounts must opt in explicitly. Missing /
unparseable config disables embedding-based intent classification for
that account.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class EmbeddingSettings:
    """Embedding configuration for one account."""

    model: str
    api_base: str | None = None
    timeout: float = 10.0
    max_input_length: int = 8000

    @classmethod
    def from_account_settings(cls, settings: dict[str, Any] | None) -> EmbeddingSettings | None:
        """Extract from ``account_embedding_settings.config`` JSONB.

        Returns ``None`` when no model is configured (feature disabled
        for the account).

        Schema::

            {
              "embedding": {
                "model": "ollama/nomic-embed-text",
                "api_base": null,
                "timeout": 10.0,
                "max_input_length": 8000
              }
            }
        """
        if not settings:
            return None
        embedding = settings.get("embedding")
        if not isinstance(embedding, dict):
            return None
        model = embedding.get("model")
        if not isinstance(model, str) or not model:
            return None
        return cls(
            model=model,
            api_base=embedding.get("api_base") or None,
            timeout=float(embedding.get("timeout", 10.0)),
            max_input_length=int(embedding.get("max_input_length", 8000)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "api_base": self.api_base,
            "timeout": self.timeout,
            "max_input_length": self.max_input_length,
        }
