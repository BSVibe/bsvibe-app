"""Classifier protocol + dataclasses."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

Tier = Literal["local", "cloud"]


@dataclass(frozen=True)
class ClassificationFeatures:
    """Inputs computed once per request; fed to every classifier.

    ``user_text`` / ``system_prompt`` default to empty so Bundle 1's
    positional constructions stay valid. Static heuristics ignore them;
    the LLM-backed secondary classifier from Bundle 1.5b uses them to
    build a prompt.
    """

    token_count: int
    system_prompt_chars: int
    conversation_turns: int
    code_block_count: int
    tool_count: int
    user_text: str = ""
    system_prompt: str = ""


@dataclass(frozen=True)
class ClassificationResult:
    tier: Tier
    score: int  # 0–100, higher = more complex
    strategy: str
    reason: str | None = None


@runtime_checkable
class Classifier(Protocol):
    async def classify(self, features: ClassificationFeatures) -> ClassificationResult: ...
