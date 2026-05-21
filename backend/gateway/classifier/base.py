"""Classifier protocol + dataclasses."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

Tier = Literal["local", "cloud"]


@dataclass(frozen=True)
class ClassificationFeatures:
    """Inputs computed once per request; fed to every classifier."""

    token_count: int
    system_prompt_chars: int
    conversation_turns: int
    code_block_count: int
    tool_count: int


@dataclass(frozen=True)
class ClassificationResult:
    tier: Tier
    score: int  # 0–100, higher = more complex
    strategy: str
    reason: str | None = None


@runtime_checkable
class Classifier(Protocol):
    async def classify(self, features: ClassificationFeatures) -> ClassificationResult: ...
