"""LLM-backed tie-breaker — plugs into ``LocalVsCloudClassifier.secondary``.

Sends a short prompt to a small local model (default: an Ollama-hosted
chat model) and parses a single-token verdict: ``local`` or ``cloud``.
Failures fall back to ``cloud`` as the safer default for unsure
requests — matches the [parked] guidance baked into
:class:`LocalVsCloudClassifier`.

The actual HTTP call is behind an ``LlmCompletionClient`` Protocol so
tests inject a deterministic stub and production wires a real
LiteLLM-backed client (Bundle 1's :class:`backend.gateway.llm_client.LlmClient`
exposes the same shape).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

import structlog

from backend.gateway.classifier.base import (
    ClassificationFeatures,
    ClassificationResult,
)

logger = structlog.get_logger(__name__)


_VALID_TIERS: set[str] = {"local", "cloud"}

_CLASSIFICATION_PROMPT = """\
Classify this request's complexity. Reply ONLY with one word: local or cloud.

local: greeting, simple Q&A, short format conversion, single-line code.
cloud: multi-step reasoning, code generation across files, architecture, security, complex refactoring.

{system_context}Request: {user_text}"""


@runtime_checkable
class LlmCompletionClient(Protocol):
    """Minimal slice of an LLM client — single-prompt completion."""

    async def complete(
        self,
        *,
        prompt: str,
        max_tokens: int,
        temperature: float,
    ) -> str: ...


class LLMClassifier:
    def __init__(
        self,
        *,
        llm: LlmCompletionClient,
        model: str,
        api_base: str | None = None,
        timeout: float = 5.0,
        user_text_max: int = 500,
        system_prompt_max: int = 200,
    ) -> None:
        self._llm = llm
        self._model = model
        self._api_base = api_base
        self._timeout = timeout
        self._user_text_max = user_text_max
        self._system_prompt_max = system_prompt_max

    async def classify(self, features: ClassificationFeatures) -> ClassificationResult:
        prompt = self._build_prompt(
            features.user_text[: self._user_text_max],
            features.system_prompt[: self._system_prompt_max],
        )
        try:
            raw = await self._llm.complete(prompt=prompt, max_tokens=10, temperature=0.0)
        except Exception:
            logger.warning("llm_classifier.unavailable", exc_info=True)
            return ClassificationResult(
                tier="cloud", score=50, strategy="llm", reason="llm_unavailable"
            )

        verdict = raw.strip().lower()
        for tier in _VALID_TIERS:
            if tier in verdict:
                return ClassificationResult(tier=tier, score=50, strategy="llm")  # type: ignore[arg-type]
        return ClassificationResult(tier="cloud", score=50, strategy="llm", reason="unparseable")

    @staticmethod
    def _build_prompt(user_text: str, system_text: str) -> str:
        system_context = f"System context: {system_text}\n" if system_text else ""
        return _CLASSIFICATION_PROMPT.format(system_context=system_context, user_text=user_text)
