"""Embedding provider abstraction + LiteLLM-backed implementation.

The protocol exposes one ``embed`` method + a ``model`` property so
callers can stamp the model name onto every persisted embedding row
(stale-detection after model swap).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

import structlog

from backend.router.embedding.settings import EmbeddingSettings

logger = structlog.get_logger(__name__)


@runtime_checkable
class EmbeddingProvider(Protocol):
    async def embed(self, texts: list[str]) -> list[list[float]]: ...

    @property
    def model(self) -> str: ...


class LiteLLMEmbeddingProvider:
    """Production provider — defers HTTP to ``litellm.aembedding``.

    Constructed per-account from :class:`EmbeddingSettings`. Imports
    litellm lazily so projects that don't enable intent classification
    don't pay the import cost.
    """

    def __init__(self, settings: EmbeddingSettings) -> None:
        self._settings = settings

    @property
    def model(self) -> str:
        return self._settings.model

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        import litellm  # noqa: PLC0415 — lazy import; see module docstring

        truncated = [t[: self._settings.max_input_length] for t in texts]
        response = await litellm.aembedding(
            model=self._settings.model,
            input=truncated,
            api_base=self._settings.api_base,
            timeout=self._settings.timeout,
        )
        return [item["embedding"] for item in response.data]


def build_provider(settings: EmbeddingSettings | None) -> EmbeddingProvider | None:
    """Factory — returns ``None`` when no model is configured."""
    if settings is None:
        return None
    return LiteLLMEmbeddingProvider(settings)
