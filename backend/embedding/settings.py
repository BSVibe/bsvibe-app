"""Embedding configuration — per-account override over a deployment default.

Stored as JSONB on ``account_embedding_settings.config``. Each account
may pick its own embedding model — different accounts can use different
providers (Ollama, OpenAI, Cohere). The chosen model is recorded on
every ``intent_examples.embedding_model`` row so we can detect stale
embeddings after a model swap.

That per-account row is an OVERRIDE, not the only source. It has no
authoring surface anywhere in the product (prod 2026-08-24: 0 rows, and
``EmbeddingSettingsRepository.upsert`` has exactly one caller — a unit
test), so treating its absence as "feature off" made intent
classification unreachable: the founder could define intents through
REST/MCP, they were persisted with ``embedding=None``, and
``classified_intent`` rules could never fire. The deployment's knowledge
embedding model (``Settings.knowledge_embedding_model``) is therefore the
fallback — the same model the note index already runs on, for the same
stated reason (``backend/config.py``: knowledge search "is not opt-in per
workspace"). Neither configured → ``None``, and the feature is a clean
no-op rather than a silent one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from backend.config import Settings


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

    @classmethod
    def from_deployment(cls, settings: Settings) -> EmbeddingSettings | None:
        """The deployment's embedding model, or ``None`` when unset.

        Reads the knowledge-index knobs deliberately: one deployment runs ONE
        embedding model, and a second knob would let the note index and the
        intent index drift onto different vector spaces while every test
        stayed green."""
        model = (settings.knowledge_embedding_model or "").strip()
        if not model:
            return None
        return cls(
            model=model,
            api_base=settings.knowledge_embedding_api_base,
            timeout=settings.knowledge_embedding_timeout_s,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "api_base": self.api_base,
            "timeout": self.timeout,
            "max_input_length": self.max_input_length,
        }


def resolve_embedding_settings(
    account_config: dict[str, Any] | None, settings: Settings
) -> EmbeddingSettings | None:
    """The embedding settings in force for one account.

    Precedence: the account's own ``account_embedding_settings.config`` row
    wins (a founder who set one keeps it), else the deployment model, else
    ``None``.

    Both ends of the wire resolve HERE — the authoring side that embeds an
    intent's examples and the classification side that matches work against
    them. If they resolved separately they could disagree about which model is
    in force, and examples would land in a vector space nothing ever searches
    (the search is scoped by ``embedding_model``), with every unit test green.
    """
    return EmbeddingSettings.from_account_settings(account_config) or (
        EmbeddingSettings.from_deployment(settings)
    )


__all__ = [
    "EmbeddingSettings",
    "resolve_embedding_settings",
]
